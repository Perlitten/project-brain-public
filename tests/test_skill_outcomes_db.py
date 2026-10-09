"""PostgreSQL contract tests for evidence-linked L4 skill outcomes."""

import asyncio
import os
import uuid

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from brain.config.settings import settings
from brain.database import harness_models  # noqa: F401
from brain.database.harness_models import AgentTask, AgentTaskArtifact, AgentValidationResult
from brain.database.migrations import _ensure_skill_outcomes_table
from brain.database.models import Base, MemorySkill, SkillOutcome
from brain.memory.skill_outcomes import SkillOutcomeConflict, record_skill_outcome


@pytest_asyncio.fixture
async def skill_db(monkeypatch):
    engine = create_async_engine(settings.DATABASE_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE SCHEMA IF NOT EXISTS harness"))
            await conn.run_sync(Base.metadata.create_all)
            await _ensure_skill_outcomes_table(conn)
    except (OSError, ConnectionError, asyncpg.PostgresError):
        await engine.dispose()
        if os.environ.get("GITHUB_ACTIONS"):
            raise
        pytest.skip("PostgreSQL is unavailable locally")

    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("brain.memory.skill_outcomes.async_session_factory", sessions)
    task_id = uuid.uuid4()
    async with sessions() as session:
        task = AgentTask(id=task_id, title="skill outcome test", goal="verify skill", repo_path="/app")
        skill = MemorySkill(
            name=f"outcome-test-{task_id}", description="test", triggers=["verify"],
            workflow=[{"step": 1}], source_episode_ids=[], source_learning_ids=[], repo_scope="/app",
        )
        session.add_all([task, skill])
        await session.flush()
        artifact = AgentTaskArtifact(task_id=task_id, kind="test-report", path_or_uri=f"test://{task_id}")
        session.add(artifact)
        await session.flush()
        validation = AgentValidationResult(
            task_id=task_id, validator="pytest", status="pass", exit_code=0, artifact_id=artifact.id,
        )
        session.add(validation)
        await session.commit()
        skill_id, artifact_id, validation_id = skill.id, artifact.id, validation.id

    def args(**overrides):
        value = {
            "skill_id": skill_id, "task_id": str(task_id), "validation_id": validation_id,
            "outcome": "success",
            "evidence": {"artifact_ids": [artifact_id], "source_refs": [f"test://{task_id}"]},
        }
        value.update(overrides)
        return value

    yield type("SkillDb", (), {"sessions": sessions, "args": args, "task_id": task_id,
                                "skill_id": skill_id, "validation_id": validation_id})

    async with engine.begin() as conn:
        await conn.execute(delete(SkillOutcome).where(SkillOutcome.task_id == task_id))
        await conn.execute(delete(AgentValidationResult).where(AgentValidationResult.task_id == task_id))
        await conn.execute(delete(AgentTaskArtifact).where(AgentTaskArtifact.task_id == task_id))
        await conn.execute(delete(AgentTask).where(AgentTask.id == task_id))
        await conn.execute(delete(MemorySkill).where(MemorySkill.id == skill_id))
    await engine.dispose()


@pytest.mark.asyncio
async def test_identical_concurrent_requests_are_idempotent(skill_db):
    first, second = await asyncio.gather(
        record_skill_outcome(**skill_db.args()), record_skill_outcome(**skill_db.args())
    )
    assert {first["idempotent"], second["idempotent"]} == {False, True}
    async with skill_db.sessions() as session:
        skill = await session.get(MemorySkill, skill_db.skill_id)
        assert (skill.times_used, skill.times_successful) == (1, 1)
        assert (await session.execute(select(func.count()).select_from(SkillOutcome))).scalar_one() == 1


@pytest.mark.asyncio
async def test_changed_payload_conflicts_without_counter_increment(skill_db):
    await record_skill_outcome(**skill_db.args())
    with pytest.raises(SkillOutcomeConflict):
        await record_skill_outcome(**skill_db.args(evidence={"artifact_ids": [999], "source_refs": ["other://x"]}))
    async with skill_db.sessions() as session:
        skill = await session.get(MemorySkill, skill_db.skill_id)
        assert (skill.times_used, skill.times_successful) == (1, 1)


@pytest.mark.asyncio
async def test_wrong_scope_and_failure_counters(skill_db):
    async with skill_db.sessions() as session:
        skill = await session.get(MemorySkill, skill_db.skill_id)
        skill.repo_scope = "/foreign"
        await session.commit()
    with pytest.raises(PermissionError):
        await record_skill_outcome(**skill_db.args())
    async with skill_db.sessions() as session:
        skill = await session.get(MemorySkill, skill_db.skill_id)
        skill.repo_scope = "/app"
        await session.commit()
    result = await record_skill_outcome(**skill_db.args(outcome="failure"))
    assert result["outcome"] == "failure"
    async with skill_db.sessions() as session:
        skill = await session.get(MemorySkill, skill_db.skill_id)
        assert (skill.times_used, skill.times_successful) == (1, 0)


@pytest.mark.asyncio
async def test_database_constraints_reject_orphaned_outcome(skill_db):
    async with skill_db.sessions() as session:
        session.add(SkillOutcome(skill_id=skill_db.skill_id, task_id=uuid.uuid4(), validation_id=skill_db.validation_id,
                                 outcome="success", evidence={"artifact_ids": [1], "source_refs": ["x"]}))
        with pytest.raises(IntegrityError):
            await session.commit()
