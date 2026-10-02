from unittest.mock import AsyncMock

import pytest

from brain.database.migrations import (
    _backfill_non_current_knowledge_authority,
    _cleanup_legacy_memory_probes,
    _ensure_decision_repo_scope,
    _ensure_decision_title_uniqueness,
    _ensure_rule_repo_scope,
)


@pytest.mark.asyncio
async def test_cleanup_legacy_memory_probes_is_narrow():
    connection = AsyncMock()

    await _cleanup_legacy_memory_probes(connection)

    statement = str(connection.execute.await_args.args[0])
    assert "MCP stdio E2E probe%" in statement
    assert "E2E memory probe%" in statement
    assert "DELETE FROM decisions" in statement


@pytest.mark.asyncio
async def test_decision_repo_scope_column_is_idempotent():
    connection = AsyncMock()

    await _ensure_decision_repo_scope(connection)

    statement = str(connection.execute.await_args.args[0])
    assert "ADD COLUMN IF NOT EXISTS repo_path VARCHAR(1024)" in statement


@pytest.mark.asyncio
async def test_rule_repo_scope_column_and_index_are_idempotent():
    connection = AsyncMock()

    await _ensure_rule_repo_scope(connection)

    statements = [str(call.args[0]) for call in connection.execute.await_args_list]
    assert "ALTER TABLE rules" in statements[0]
    assert "ADD COLUMN IF NOT EXISTS repo_path VARCHAR(1024)" in statements[0]
    assert "CREATE INDEX IF NOT EXISTS ix_rules_repo_path" in statements[1]


@pytest.mark.asyncio
async def test_decision_title_uniqueness_uses_repo_scoped_normalized_index():
    connection = AsyncMock()

    await _ensure_decision_title_uniqueness(connection)

    statements = [str(call.args[0]) for call in connection.execute.await_args_list]
    assert "DROP INDEX IF EXISTS uq_decisions_normalized_title" in statements[0]
    assert "CREATE UNIQUE INDEX IF NOT EXISTS uq_decisions_normalized_title" in statements[1]
    assert "lower(btrim(title))" in statements[1]
    assert "COALESCE(lower(btrim(repo_path)), '')" in statements[1]


@pytest.mark.asyncio
async def test_non_current_authority_backfill_is_path_bounded_and_idempotent():
    connection = AsyncMock()

    await _backfill_non_current_knowledge_authority(connection)

    statements = [str(call.args[0]) for call in connection.execute.await_args_list]
    assert len(statements) == 2
    for statement in statements:
        assert ".agent-factory/tasks/%" in statement
        assert "docs/audits/%" in statement
        assert "docs/design/sources/%" in statement
        assert "NOT LIKE :prefix_like" in statement
