"""Postgres-backed consolidation tests: idempotent L1 collection, atomic L2 writes, approval flow.

Skipped locally without Postgres; required under GITHUB_ACTIONS (same contract as test_indexing_reliability).
"""

import json
import os
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from brain.config.settings import settings
from brain.database import harness_models  # noqa: F401  (registers harness tables on Base.metadata)
from brain.database.harness_models import AgentTask, AgentTaskEvent
from brain.database.migrations import _ensure_memory_episode_ledger
from brain.database.models import Base, Learning, MemoryEpisode, MemoryEpisodeEvent
from brain.embeddings.constants import EMBEDDING_DIMENSION
from brain.embeddings.pgvector_sql import pgvector_index_dimension
from brain.memory import consolidation
from brain.memory.consolidation import EpisodeStateError, approve_episode, reject_episode
from brain.memory.learning_store import LearningStore

STATEMENT = "NVIDIA embeddings are 2048-dimensional"
_MEMORY_TABLES = "memory_episode_events, memory_episodes, memory_learnings"


@pytest_asyncio.fixture
async def memory_db(monkeypatch):
    engine = create_async_engine(settings.DATABASE_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.execute(text("CREATE SCHEMA IF NOT EXISTS harness"))
            await conn.run_sync(Base.metadata.create_all)
            # create_all never alters an existing table; upgrade it like startup does.
            await _ensure_memory_episode_ledger(conn)
            await conn.execute(text(f"TRUNCATE {_MEMORY_TABLES} RESTART IDENTITY CASCADE"))
    except (OSError, ConnectionError):
        await engine.dispose()
        if os.environ.get("GITHUB_ACTIONS"):
            raise
        pytest.skip("PostgreSQL is unavailable locally")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("brain.memory.consolidation.async_session_factory", sessions)
    monkeypatch.setattr("brain.memory.learning_store.async_session_factory", sessions)

    vector = [1.0] + [0.0] * (pgvector_index_dimension(EMBEDDING_DIMENSION) - 1)

    async def embed(_text, **_kwargs):
        return list(vector)

    async def embed_batch(texts, **_kwargs):
        return [list(vector) for _ in texts]

    embedder = SimpleNamespace(embed=embed, embed_batch=embed_batch, model="test-embedder")

    async def summarize(prompt, **_kwargs):
        if "contradicts" in prompt:
            return json.dumps({"contradicts": False, "reason": ""})
        return json.dumps({"statement": STATEMENT, "category": "infra", "confidence": 0.9, "severity": "low"})

    monkeypatch.setattr("brain.memory.consolidation.get_embedding_provider", lambda: embedder)
    monkeypatch.setattr("brain.llm.get_embedding_provider", lambda: embedder)
    monkeypatch.setattr(
        "brain.memory.consolidation.get_summarizer_provider", lambda: SimpleNamespace(summarize=summarize)
    )

    async with sessions() as session:
        task = AgentTask(title="consolidation-test", goal="test", repo_path="/tmp/consolidation-test")
        session.add(task)
        await session.commit()
        task_id = task.id

    async def add_events(n):
        async with sessions() as session:
            events = [
                AgentTaskEvent(
                    task_id=task_id,
                    event_type="note",
                    actor="test",
                    classification="learning",
                    payload_json={"summary": "embedding dim is 2048"},
                )
                for _ in range(n)
            ]
            session.add_all(events)
            await session.commit()
            return [e.id for e in events]

    async def count(model):
        async with sessions() as session:
            return (await session.execute(select(func.count()).select_from(model))).scalar_one()

    # L1 events are never truncated (harness tables are shared): earlier test
    # runs leave unconsumed events behind. Collect only events created after
    # this fixture's setup — no skew buffer: the previous test's events land
    # inside even a one-second window.
    since = datetime.now(timezone.utc)

    async def run(**kwargs):
        return await consolidation._run_consolidation_unlocked(since=since, **kwargs)

    yield SimpleNamespace(sessions=sessions, add_events=add_events, count=count, run=run)

    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {_MEMORY_TABLES} RESTART IDENTITY CASCADE"))
        await conn.execute(text("DELETE FROM harness.agent_tasks WHERE id = :id"), {"id": task_id})
    await engine.dispose()


@pytest.mark.asyncio
async def test_consolidation_twice_creates_no_duplicate_episodes(memory_db):
    event_ids = await memory_db.add_events(2)

    first = await memory_db.run()
    second = await memory_db.run()

    assert first["l1_events"] == 2 and first["episodes_created"] == 1 and len(first["promoted"]) == 1
    assert second["l1_events"] == 0 and second["episodes_created"] == 0
    assert await memory_db.count(MemoryEpisode) == 1
    assert await memory_db.count(MemoryEpisodeEvent) == 2
    assert await memory_db.count(Learning) == 1
    async with memory_db.sessions() as session:
        episode = (await session.execute(select(MemoryEpisode))).scalar_one()
    assert episode.status == "promoted"
    assert episode.distilled_summary == STATEMENT
    assert sorted(episode.source_event_ids) == sorted(event_ids)
    assert episode.promoted_to_learning_id == first["promoted"][0]["learning_id"]

    # A new L1 event is still picked up; already-consumed ones are not.
    await memory_db.add_events(1)
    third = await memory_db.run()
    assert third["l1_events"] == 1
    assert await memory_db.count(MemoryEpisode) == 2


@pytest.mark.asyncio
async def test_failed_l2_write_rolls_back_and_events_are_retried(memory_db):
    await memory_db.add_events(2)

    with patch.object(LearningStore, "add_learning", AsyncMock(side_effect=RuntimeError("db hiccup"))):
        failed = await memory_db.run()

    assert failed["episodes_created"] == 0
    assert failed["errors"] and "db hiccup" in failed["errors"][0]["error"]
    assert await memory_db.count(MemoryEpisode) == 0
    assert await memory_db.count(MemoryEpisodeEvent) == 0

    retried = await memory_db.run()
    assert retried["l1_events"] == 2 and retried["episodes_created"] == 1
    async with memory_db.sessions() as session:
        episode = (await session.execute(select(MemoryEpisode))).scalar_one()
    assert episode.distilled_summary == STATEMENT


@pytest.mark.asyncio
async def test_duplicate_episode_keeps_duplicate_of(memory_db):
    existing_id = await LearningStore.add_learning(statement=STATEMENT, category="infra", confidence=0.9)
    await memory_db.add_events(2)

    report = await memory_db.run()

    assert report["rejected"][0]["duplicate_of"] == existing_id
    async with memory_db.sessions() as session:
        episode = (await session.execute(select(MemoryEpisode))).scalar_one()
    assert episode.status == "duplicate"
    assert episode.duplicate_of_learning_id == existing_id
    assert await memory_db.count(Learning) == 1


@pytest.mark.asyncio
async def test_pending_episode_approve_and_reject_flow(memory_db):
    await memory_db.add_events(2)
    report = await memory_db.run(require_approval=True)
    assert len(report["needs_approval"]) == 1
    episode_id = report["needs_approval"][0]["episode_id"]
    assert await memory_db.count(Learning) == 0

    decided = await approve_episode(episode_id, "checked against the NIM docs")
    assert decided["status"] == "promoted" and decided["learning_id"]
    assert await memory_db.count(Learning) == 1
    with pytest.raises(EpisodeStateError):
        await approve_episode(episode_id)
    with pytest.raises(EpisodeStateError):
        await reject_episode(episode_id)
    with pytest.raises(LookupError):
        await approve_episode(999_999)

    await memory_db.add_events(2)
    second = await memory_db.run(require_approval=True)
    # G1 now sees the approved learning, so this cluster is a duplicate, not pending.
    assert second["needs_approval"] == []
    async with memory_db.sessions() as session:
        promoted = await session.get(MemoryEpisode, episode_id)
    assert promoted.promoted_to_learning_id == decided["learning_id"]
    assert promoted.gate_reasons[-1] == "approved by human: checked against the NIM docs"


async def _add_task_events(sessions, repo_path, n):
    """Create an AgentTask for a repo and n L1 events on it; return event ids."""
    async with sessions() as session:
        task = AgentTask(title=f"task-{repo_path}", goal="test", repo_path=repo_path)
        session.add(task)
        await session.commit()
        task_id = task.id
        events = [
            AgentTaskEvent(
                task_id=task_id,
                event_type="note",
                actor="test",
                classification="learning",
                payload_json={"summary": f"note from {repo_path}"},
            )
            for _ in range(n)
        ]
        session.add_all(events)
        await session.commit()
        return task_id, [e.id for e in events]


@pytest.mark.asyncio
async def test_episode_and_learning_inherit_task_repo_scope(memory_db):
    await memory_db.add_events(2)
    await memory_db.run()
    async with memory_db.sessions() as session:
        episode = (await session.execute(select(MemoryEpisode))).scalar_one()
        learning = (await session.execute(select(Learning))).scalar_one()
    # Scope comes from the L1 events' agent_tasks.repo_path, normalized.
    assert episode.repo_scope == "/tmp/consolidation-test"
    assert learning.repo_scope == "/tmp/consolidation-test"


@pytest.mark.asyncio
async def test_rejected_episode_events_reopen_on_new_event_same_repo(memory_db):
    await memory_db.add_events(2)
    await memory_db.run(require_approval=True)
    async with memory_db.sessions() as session:
        episode = (await session.execute(select(MemoryEpisode))).scalar_one()
    await reject_episode(episode.id, "not durable")
    assert await memory_db.count(MemoryEpisodeEvent) == 2

    await memory_db.add_events(1)
    await memory_db.run()

    # The rejected episode's ledger rows were released: all 3 events re-clustered
    # into one new episode and only the new episode holds ledger rows.
    async with memory_db.sessions() as session:
        episodes = (await session.execute(select(MemoryEpisode).order_by(MemoryEpisode.id))).scalars().all()
        ledger = (await session.execute(select(MemoryEpisodeEvent))).scalars().all()
    assert len(episodes) == 2
    assert episodes[0].status == "rejected"
    assert episodes[1].status == "promoted"
    assert len(episodes[1].source_event_ids) == 3
    assert {row.episode_id for row in ledger} == {episodes[1].id}


@pytest.mark.asyncio
async def test_rejected_episode_not_reopened_by_other_repo(memory_db):
    task_b, _events_b = await _add_task_events(memory_db.sessions, "/tmp/other-repo", 2)
    await memory_db.run(require_approval=True)
    async with memory_db.sessions() as session:
        episode = (await session.execute(select(MemoryEpisode))).scalar_one()
    await reject_episode(episode.id, "repo-B only")

    # A fresh event in a *different* repository does not re-open it.
    await memory_db.add_events(1)
    await memory_db.run()

    async with memory_db.sessions() as session:
        episodes = (await session.execute(select(MemoryEpisode).order_by(MemoryEpisode.id))).scalars().all()
        ledger = (await session.execute(select(MemoryEpisodeEvent))).scalars().all()
    assert len(episodes) == 2
    # The rejected episode kept its 2 events; the fresh event formed its own episode.
    assert len(episodes[1].source_event_ids) == 1
    episode_event_ids = {row.event_id for row in ledger if row.episode_id == episodes[0].id}
    assert len(episode_event_ids) == 2


@pytest.mark.asyncio
async def test_rejected_episode_stays_consumed_outside_reopen_window(memory_db):
    await memory_db.add_events(2)
    await memory_db.run(require_approval=True)
    async with memory_db.sessions() as session:
        episode = (await session.execute(select(MemoryEpisode))).scalar_one()
    await reject_episode(episode.id, "not durable")

    # Push the rejection outside MEMORY_EPISODE_REOPEN_DAYS.
    async with memory_db.sessions() as session:
        await session.execute(
            text(
                "UPDATE memory_episodes SET updated_at = now() - interval '60 days' "
                "WHERE id = :id"
            ),
            {"id": episode.id},
        )
        await session.commit()

    await memory_db.add_events(1)
    await memory_db.run()

    async with memory_db.sessions() as session:
        episodes = (await session.execute(select(MemoryEpisode).order_by(MemoryEpisode.id))).scalars().all()
        ledger = (await session.execute(select(MemoryEpisodeEvent))).scalars().all()
    assert len(episodes) == 2
    assert len(episodes[1].source_event_ids) == 1
    episode_event_ids = {row.event_id for row in ledger if row.episode_id == episodes[0].id}
    assert len(episode_event_ids) == 2


@pytest.mark.asyncio
async def test_reopen_disabled_keeps_events_consumed(memory_db, monkeypatch):
    monkeypatch.setattr(settings, "MEMORY_EPISODE_REOPEN_DAYS", 0)
    await memory_db.add_events(2)
    await memory_db.run(require_approval=True)
    async with memory_db.sessions() as session:
        episode = (await session.execute(select(MemoryEpisode))).scalar_one()
    await reject_episode(episode.id, "not durable")

    await memory_db.add_events(1)
    await memory_db.run()

    async with memory_db.sessions() as session:
        ledger = (await session.execute(select(MemoryEpisodeEvent))).scalars().all()
    episode_event_ids = {row.event_id for row in ledger if row.episode_id == episode.id}
    assert len(episode_event_ids) == 2


@pytest.mark.asyncio
async def test_null_scope_episode_reopened_by_null_scope_event(memory_db):
    # NULL↔NULL: a rejected unscoped episode re-opens on a fresh unscoped event.
    await _add_task_events(memory_db.sessions, "", 2)
    await memory_db.run(require_approval=True)
    async with memory_db.sessions() as session:
        episode = (await session.execute(select(MemoryEpisode))).scalar_one()
    assert episode.repo_scope is None
    await reject_episode(episode.id, "unscoped rejection")

    await _add_task_events(memory_db.sessions, "", 1)
    await memory_db.run()

    async with memory_db.sessions() as session:
        episodes = (await session.execute(select(MemoryEpisode).order_by(MemoryEpisode.id))).scalars().all()
        ledger = (await session.execute(select(MemoryEpisodeEvent))).scalars().all()
    assert len(episodes) == 2
    assert episodes[0].status == "rejected"
    assert len(episodes[1].source_event_ids) == 3
    assert {row.episode_id for row in ledger} == {episodes[1].id}


@pytest.mark.asyncio
async def test_null_scope_episode_not_reopened_by_scoped_event(memory_db):
    # NULL matches only NULL: a scoped event must not re-open an unscoped
    # episode — otherwise any repo's activity could resurrect unscoped memory.
    await _add_task_events(memory_db.sessions, "", 2)
    await memory_db.run(require_approval=True)
    async with memory_db.sessions() as session:
        episode = (await session.execute(select(MemoryEpisode))).scalar_one()
    assert episode.repo_scope is None
    await reject_episode(episode.id, "unscoped rejection")

    await memory_db.add_events(1)  # scoped to /tmp/consolidation-test
    await memory_db.run()

    async with memory_db.sessions() as session:
        episodes = (await session.execute(select(MemoryEpisode).order_by(MemoryEpisode.id))).scalars().all()
        ledger = (await session.execute(select(MemoryEpisodeEvent))).scalars().all()
    assert len(episodes) == 2
    # The rejected episode kept its 2 events; the fresh event formed its own.
    assert len(episodes[1].source_event_ids) == 1
    episode_event_ids = {row.event_id for row in ledger if row.episode_id == episodes[0].id}
    assert len(episode_event_ids) == 2


@pytest.mark.asyncio
async def test_scoped_episode_not_reopened_by_null_scope_event(memory_db):
    # Symmetric direction: an unscoped event must not re-open a scoped episode.
    await memory_db.add_events(2)  # scoped to /tmp/consolidation-test
    await memory_db.run(require_approval=True)
    async with memory_db.sessions() as session:
        episode = (await session.execute(select(MemoryEpisode))).scalar_one()
    assert episode.repo_scope == "/tmp/consolidation-test"
    await reject_episode(episode.id, "scoped rejection")

    await _add_task_events(memory_db.sessions, "", 1)  # unscoped event
    await memory_db.run()

    async with memory_db.sessions() as session:
        episodes = (await session.execute(select(MemoryEpisode).order_by(MemoryEpisode.id))).scalars().all()
        ledger = (await session.execute(select(MemoryEpisodeEvent))).scalars().all()
    assert len(episodes) == 2
    assert len(episodes[1].source_event_ids) == 1
    assert episodes[1].repo_scope is None
    episode_event_ids = {row.event_id for row in ledger if row.episode_id == episodes[0].id}
    assert len(episode_event_ids) == 2


def _promotable_candidate(event_ids):
    return consolidation.ConsolidationCandidate(
        statement=STATEMENT,
        category="infra",
        confidence=0.9,
        evidence=[{"event_id": event_id} for event_id in event_ids],
    )


@pytest.mark.asyncio
async def test_promote_candidate_derives_modal_repo_scope(memory_db):
    # Manual L3 promotion is scoped like the approve path: modal repo of the
    # candidate's evidence events (2 events in repo-a beat 1 in repo-b).
    _, a_events = await _add_task_events(memory_db.sessions, "/tmp/promote-repo-a/", 2)
    _, b_events = await _add_task_events(memory_db.sessions, "/tmp/promote-repo-b", 1)
    gate = consolidation.GateResult(outcome=consolidation.GateOutcome.PROMOTE)
    learning_id = await consolidation.promote_candidate(
        _promotable_candidate(a_events + b_events), gate, run_id="manual"
    )
    async with memory_db.sessions() as session:
        learning = await session.get(Learning, learning_id)
    assert learning.repo_scope == "/tmp/promote-repo-a"


@pytest.mark.asyncio
async def test_promote_candidate_explicit_scope_wins_and_underivable_stays_null(memory_db):
    _, events = await _add_task_events(memory_db.sessions, "/tmp/promote-repo-a", 2)
    gate = consolidation.GateResult(outcome=consolidation.GateOutcome.PROMOTE)
    explicit_id = await consolidation.promote_candidate(
        _promotable_candidate(events), gate, repo_scope="/tmp/explicit-repo/"
    )
    orphan_id = await consolidation.promote_candidate(
        consolidation.ConsolidationCandidate(statement="orphan learning", evidence=[{"event_id": -1}]), gate
    )
    async with memory_db.sessions() as session:
        explicit = await session.get(Learning, explicit_id)
        orphan = await session.get(Learning, orphan_id)
    assert explicit.repo_scope == "/tmp/explicit-repo"
    assert orphan.repo_scope is None
