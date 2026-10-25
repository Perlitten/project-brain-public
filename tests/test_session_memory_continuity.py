"""End-to-end memory continuity: what a session writes, the next one can read.

Reproduces the prod gap from the memory-delivery audit: 132 handoffs and 65
open tasks sat in the database while `prepare_task_context` returned none of
them, and a single verified lesson could never reach L3 through the G2 gate.
"""
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from brain.context import runtime_context_builder
from brain.database.harness_models import AgentTask, MemoryHandoff
from brain.database.models import Learning
from brain.memory import learning_store
from brain.memory.repo_scope import normalize_repo_scope


@pytest_asyncio.fixture
async def ctx_db(tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'ctx.sqlite3'}",
        execution_options={"schema_translate_map": {"harness": None}},
    )
    async with engine.begin() as conn:
        for table in (Learning.__table__, AgentTask.__table__, MemoryHandoff.__table__):
            await conn.run_sync(table.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(runtime_context_builder, "async_session_factory", factory)
    monkeypatch.setattr(learning_store, "async_session_factory", factory)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_handoff_written_last_session_is_read_next(ctx_db):
    """Session 1 records a handoff + open task; session 2 sees them in the
    continuation block that prepare_task_context now returns."""
    task = AgentTask(title="M4 ledger migration", goal="ship it", status="in_progress",
                     repo_path="/RepoA", priority="P1")
    async with ctx_db() as session:
        session.add(task)
        session.add(MemoryHandoff(session_id="session-1",
                                  summary="stopped at schema_migrations ledger"))
        await session.commit()

    continuation = await runtime_context_builder.RuntimeContextBuilder._load_continuation("/RepoA", {})

    assert continuation is not None
    assert continuation["open_tasks"][0]["title"] == "M4 ledger migration"
    assert "schema_migrations" in continuation["handoffs"][0]["summary"]


@pytest.mark.asyncio
async def test_done_tasks_and_other_repo_handoffs_are_hidden(ctx_db):
    async with ctx_db() as session:
        session.add(AgentTask(title="finished", goal="g", status="completed", repo_path="/RepoA"))
        session.add(AgentTask(title="other repo", goal="g", status="in_progress", repo_path="/RepoB"))
        session.add(MemoryHandoff(session_id="s", summary="global note",
                                  task_id=None))
        await session.commit()

    continuation = await runtime_context_builder.RuntimeContextBuilder._load_continuation("/RepoA", {})

    assert continuation is not None
    titles = {t["title"] for t in continuation["open_tasks"]}
    assert "finished" not in titles and "other repo" not in titles
    # task-less handoffs stay repository-agnostic and visible
    assert continuation["handoffs"][0]["summary"] == "global note"


@pytest.mark.asyncio
async def test_explicit_learning_reaches_active_l3(ctx_db):
    """One verified lesson written through the store lands directly in active
    L3 — the path the new record_learning MCP tool uses."""
    learning_id = await learning_store.LearningStore.add_learning(
        statement="Schema ledger lives in brain_schema_migrations",
        category="agent_note",
        confidence=0.9,
        evidence=[{"kind": "agent_explicit"}],
        repo_scope="/RepoA",
        status="active",
    )
    async with ctx_db() as session:
        row = (await session.execute(select(Learning).where(Learning.id == learning_id))).scalar_one()
        assert row.status == "active"
        assert row.repo_scope == normalize_repo_scope("/RepoA")
    active = await learning_store.LearningStore.list_active_learnings("/RepoA")
    assert any("brain_schema_migrations" in item.statement for item in active)
