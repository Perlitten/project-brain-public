"""Postgres-backed L4 skill scoping: repo_scope filtering in matching and context selection.

Skipped locally without Postgres; required under GITHUB_ACTIONS.
"""

import os
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from brain.config.settings import settings
from brain.database import harness_models  # noqa: F401  (registers harness tables on Base.metadata)
from brain.database.models import Base, MemorySkill
from brain.embeddings.constants import EMBEDDING_DIMENSION
from brain.embeddings.pgvector_sql import pgvector_index_dimension
from brain.memory import skill_store

_DIMS = pgvector_index_dimension(EMBEDDING_DIMENSION)


@pytest_asyncio.fixture
async def skill_db(monkeypatch):
    engine = create_async_engine(settings.DATABASE_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.execute(text("CREATE SCHEMA IF NOT EXISTS harness"))
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(text("TRUNCATE memory_skills RESTART IDENTITY CASCADE"))
    except (OSError, ConnectionError):
        await engine.dispose()
        if os.environ.get("GITHUB_ACTIONS"):
            raise
        pytest.skip("PostgreSQL is unavailable locally")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("brain.memory.skill_store.async_session_factory", sessions)

    # Keyword-keyed deterministic embeddings so ranking assertions are stable:
    # texts about "deploy" embed near other "deploy" texts, likewise "migrate".
    def _vec_for(value: str):
        vec = [0.0] * _DIMS
        folded = value.lower()
        if "deploy" in folded:
            vec[0] = 1.0
        elif "migrate" in folded:
            vec[1] = 1.0
        else:
            vec[2] = 1.0
        return vec

    async def embed(text_value, **_kwargs):
        return _vec_for(text_value)

    embedder = SimpleNamespace(embed=embed, model="test-embedder")
    monkeypatch.setattr("brain.llm.get_embedding_provider", lambda: embedder)
    yield SimpleNamespace(sessions=sessions)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE memory_skills RESTART IDENTITY CASCADE"))
    await engine.dispose()


async def _seed_skills():
    await skill_store.create_skill(
        name="repo-a-deploy",
        description="deploy pipeline for repo A",
        triggers=["deploy"],
        workflow=[{"step": 1, "action": "run deploy.sh"}],
        source_episode_ids=[],
        source_learning_ids=[],
        confidence=0.8,
        repo_scope="/tmp/repo-a",
    )
    await skill_store.create_skill(
        name="repo-a-migrate",
        description="database migration helper for repo A",
        triggers=["migrate"],
        workflow=[{"step": 1, "action": "alembic upgrade"}],
        source_episode_ids=[],
        source_learning_ids=[],
        confidence=0.8,
        repo_scope="/tmp/repo-a",
    )
    await skill_store.create_skill(
        name="repo-b-deploy",
        description="deploy pipeline for repo B",
        triggers=["deploy"],
        workflow=[{"step": 1, "action": "run b-deploy.sh"}],
        source_episode_ids=[],
        source_learning_ids=[],
        confidence=0.8,
        repo_scope="/tmp/repo-b",
    )
    await skill_store.create_skill(
        name="global-deploy",
        description="generic deploy checklist",
        triggers=["deploy"],
        workflow=[{"step": 1, "action": "checklist"}],
        source_episode_ids=[],
        source_learning_ids=[],
        confidence=0.8,
        repo_scope=None,
    )


@pytest.mark.asyncio
async def test_match_scoped_to_repo_excludes_other_repos(skill_db):
    await _seed_skills()
    names = {m["name"] for m in await skill_store.match_skills("deploy runbook", repo_scope="/tmp/repo-a", limit=10)}
    assert "repo-a-deploy" in names
    assert "global-deploy" in names
    assert "repo-b-deploy" not in names


@pytest.mark.asyncio
async def test_match_prefers_on_topic_over_off_topic(skill_db):
    await _seed_skills()
    matches = await skill_store.match_skills("deploy runbook", repo_scope="/tmp/repo-a", limit=2)
    names = {m["name"] for m in matches}
    # The deploy skills outrank the off-topic migration helper for a deploy query.
    assert "repo-a-deploy" in names
    assert "repo-a-migrate" not in names


@pytest.mark.asyncio
async def test_match_unscoped_query_sees_only_global(skill_db):
    await _seed_skills()
    names = {m["name"] for m in await skill_store.match_skills("deploy runbook", limit=10)}
    assert names == {"global-deploy"}


@pytest.mark.asyncio
async def test_select_skills_for_context_respects_top_and_scope(skill_db):
    await _seed_skills()
    picked = await skill_store.select_skills_for_context("deploy runbook", "/tmp/repo-b", max_count=2)
    names = {m["name"] for m in picked}
    assert len(picked) <= 2
    # Repo B sees its own deploy skill and the global one — never repo A's.
    assert "repo-b-deploy" in names
    assert "repo-a-deploy" not in names
    assert "repo-a-migrate" not in names


@pytest.mark.asyncio
async def test_create_skill_normalizes_repo_scope(skill_db):
    skill = await skill_store.create_skill(
        name="trailing-slash",
        description="d",
        triggers=[],
        workflow=[],
        source_episode_ids=[],
        source_learning_ids=[],
        confidence=0.5,
        repo_scope="/tmp/repo-c/",
    )
    assert skill.repo_scope == "/tmp/repo-c"
    names = {m["name"] for m in await skill_store.match_skills("d", repo_scope="/tmp/repo-c", limit=10)}
    # A normalized stored scope must still match a normalized query scope even
    # though the stored row's embedding is non-NULL only via the fixture.
    assert "trailing-slash" in names


@pytest.mark.asyncio
async def test_deprecated_skill_not_matched(skill_db):
    await _seed_skills()
    async with skill_db.sessions() as session:
        deprecated = (
            await session.execute(select(MemorySkill).where(MemorySkill.name == "repo-a-deploy"))
        ).scalar_one()
        deprecated.status = "deprecated"
        await session.commit()
    names = {m["name"] for m in await skill_store.match_skills("deploy runbook", repo_scope="/tmp/repo-a", limit=10)}
    assert "repo-a-deploy" not in names
