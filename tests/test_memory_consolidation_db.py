"""Postgres-backed consolidation tests: idempotent L1 collection, atomic L2 writes, approval flow.

Skipped locally without Postgres; required under GITHUB_ACTIONS (same contract as test_indexing_reliability).
"""

import json
import os
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

    yield SimpleNamespace(sessions=sessions, add_events=add_events, count=count)

    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {_MEMORY_TABLES} RESTART IDENTITY CASCADE"))
        await conn.execute(text("DELETE FROM harness.agent_tasks WHERE id = :id"), {"id": task_id})
    await engine.dispose()


@pytest.mark.asyncio
async def test_consolidation_twice_creates_no_duplicate_episodes(memory_db):
    event_ids = await memory_db.add_events(2)

    first = await consolidation._run_consolidation_unlocked()
    second = await consolidation._run_consolidation_unlocked()

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
    third = await consolidation._run_consolidation_unlocked()
    assert third["l1_events"] == 1
    assert await memory_db.count(MemoryEpisode) == 2


@pytest.mark.asyncio
async def test_failed_l2_write_rolls_back_and_events_are_retried(memory_db):
    await memory_db.add_events(2)

    with patch.object(LearningStore, "add_learning", AsyncMock(side_effect=RuntimeError("db hiccup"))):
        failed = await consolidation._run_consolidation_unlocked()

    assert failed["episodes_created"] == 0
    assert failed["errors"] and "db hiccup" in failed["errors"][0]["error"]
    assert await memory_db.count(MemoryEpisode) == 0
    assert await memory_db.count(MemoryEpisodeEvent) == 0

    retried = await consolidation._run_consolidation_unlocked()
    assert retried["l1_events"] == 2 and retried["episodes_created"] == 1
    async with memory_db.sessions() as session:
        episode = (await session.execute(select(MemoryEpisode))).scalar_one()
    assert episode.distilled_summary == STATEMENT


@pytest.mark.asyncio
async def test_duplicate_episode_keeps_duplicate_of(memory_db):
    existing_id = await LearningStore.add_learning(statement=STATEMENT, category="infra", confidence=0.9)
    await memory_db.add_events(2)

    report = await consolidation._run_consolidation_unlocked()

    assert report["rejected"][0]["duplicate_of"] == existing_id
    async with memory_db.sessions() as session:
        episode = (await session.execute(select(MemoryEpisode))).scalar_one()
    assert episode.status == "duplicate"
    assert episode.duplicate_of_learning_id == existing_id
    assert await memory_db.count(Learning) == 1


@pytest.mark.asyncio
async def test_pending_episode_approve_and_reject_flow(memory_db):
    await memory_db.add_events(2)
    report = await consolidation._run_consolidation_unlocked(require_approval=True)
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
    second = await consolidation._run_consolidation_unlocked(require_approval=True)
    # G1 now sees the approved learning, so this cluster is a duplicate, not pending.
    assert second["needs_approval"] == []
    async with memory_db.sessions() as session:
        promoted = await session.get(MemoryEpisode, episode_id)
    assert promoted.promoted_to_learning_id == decided["learning_id"]
    assert promoted.gate_reasons[-1] == "approved by human: checked against the NIM docs"
