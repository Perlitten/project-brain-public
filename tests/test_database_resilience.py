import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apps.api.helpers import get_db_counts
from brain.database import session as session_module


@pytest.fixture
def reset_process_db_initialization():
    original_initialized = session_module._db_initialized
    original_graph_ready = session_module._graph_schema_ready
    original_lock = session_module._init_db_lock
    session_module._db_initialized = False
    session_module._graph_schema_ready = False
    session_module._init_db_lock = asyncio.Lock()
    try:
        yield
    finally:
        session_module._db_initialized = original_initialized
        session_module._graph_schema_ready = original_graph_ready
        session_module._init_db_lock = original_lock


@pytest.mark.asyncio
async def test_init_db_runs_schema_path_once_for_concurrent_and_later_calls(
    reset_process_db_initialization,
):
    initializer = AsyncMock()
    graph_initializer = AsyncMock(return_value=True)

    with (
        patch.object(session_module, "_init_db_with_advisory_lock", new=initializer),
        patch.object(session_module, "_initialize_graph_schema", new=graph_initializer),
    ):
        await asyncio.gather(
            session_module.init_db(),
            session_module.init_db(),
            session_module.init_db(),
        )
        await session_module.init_db()

    initializer.assert_awaited_once()
    graph_initializer.assert_awaited_once()
    assert session_module._db_initialized is True
    assert session_module._graph_schema_ready is True


@pytest.mark.asyncio
async def test_init_db_retries_after_failed_first_attempt(reset_process_db_initialization):
    initializer = AsyncMock(side_effect=[RuntimeError("postgres unavailable"), None])
    graph_initializer = AsyncMock(return_value=True)

    with (
        patch.object(session_module, "_init_db_with_advisory_lock", new=initializer),
        patch.object(session_module, "_initialize_graph_schema", new=graph_initializer),
    ):
        with pytest.raises(RuntimeError, match="postgres unavailable"):
            await session_module.init_db()
        assert session_module._db_initialized is False

        await session_module.init_db()

    assert initializer.await_count == 2
    graph_initializer.assert_awaited_once()
    assert session_module._db_initialized is True
    assert session_module._graph_schema_ready is True


@pytest.mark.asyncio
async def test_init_db_retries_only_graph_schema_after_deferred_neo4j(
    reset_process_db_initialization,
):
    initializer = AsyncMock()
    graph_initializer = AsyncMock(side_effect=[False, True])

    with (
        patch.object(session_module, "_init_db_with_advisory_lock", new=initializer),
        patch.object(session_module, "_initialize_graph_schema", new=graph_initializer),
    ):
        await session_module.init_db()
        assert session_module._db_initialized is True
        assert session_module._graph_schema_ready is False

        await session_module.init_db()
        await session_module.init_db()

    initializer.assert_awaited_once()
    assert graph_initializer.await_count == 2
    assert session_module._graph_schema_ready is True


@pytest.mark.asyncio
async def test_get_db_counts_rolls_back_failed_transaction_and_continues():
    result = MagicMock()
    result.scalar.return_value = 7
    session = AsyncMock()
    session.execute.side_effect = [RuntimeError("deadlock detected")] + [result] * 7

    counts = await get_db_counts(session)

    assert counts["files"] == 0
    assert all(value == 7 for name, value in counts.items() if name != "files")
    session.rollback.assert_awaited_once()
    assert session.execute.await_count == 8


@pytest.mark.asyncio
async def test_get_db_counts_keeps_zero_fallback_when_rollback_also_fails():
    session = AsyncMock()
    session.execute.side_effect = RuntimeError("connection lost")
    session.rollback.side_effect = RuntimeError("connection invalidated")

    counts = await get_db_counts(session)

    assert all(value == 0 for value in counts.values())
    assert session.execute.await_count == 8
    assert session.rollback.await_count == 8
