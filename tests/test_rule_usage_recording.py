"""Recording rule usage must be idempotent-ish, bounded, and never fatal.

Accounting runs inside the context-pack build and the diff review. If a write
to this table could raise, a telemetry outage would take out the feature it was
meant to observe — so the recorder swallows failures and reports how many rows
it actually wrote.
"""

from unittest.mock import AsyncMock

import pytest

from brain.database.migrations import (
    _ensure_context_pack_composition_columns,
    _ensure_rule_usage_events_table,
)
from brain.memory import rule_usage


class _Session:
    def __init__(self, fail: bool = False):
        self.added: list = []
        self.commits = 0
        self.fail = fail

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def add_all(self, rows):
        if self.fail:
            raise RuntimeError("relation does not exist")
        self.added.extend(rows)

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_delivered_rules_are_recorded_once_each(monkeypatch):
    session = _Session()
    monkeypatch.setattr(rule_usage, "async_session_factory", lambda: session)

    written = await rule_usage.record_rules_delivered(
        ["a-rule", "b-rule", "a-rule", "  ", ""],
        repository_id=3,
        context_pack_id=42,
        principal_name="mcp:claude-code",
    )

    assert written == 2
    assert [row.rule_id for row in session.added] == ["a-rule", "b-rule"]
    assert {row.kind for row in session.added} == {"delivered"}
    assert session.added[0].context_pack_id == 42
    assert session.added[0].principal_name == "mcp:claude-code"
    assert session.commits == 1


@pytest.mark.asyncio
async def test_oversized_identifiers_are_truncated_to_the_column(monkeypatch):
    session = _Session()
    monkeypatch.setattr(rule_usage, "async_session_factory", lambda: session)

    await rule_usage.record_rules_flagged(["r" * 400], principal_name="p" * 300)

    assert len(session.added[0].rule_id) == 255
    assert len(session.added[0].principal_name) == 128


@pytest.mark.asyncio
async def test_nothing_to_record_opens_no_session(monkeypatch):
    def explode():
        raise AssertionError("no session for an empty rule list")

    monkeypatch.setattr(rule_usage, "async_session_factory", explode)

    assert await rule_usage.record_rules_delivered([]) == 0


@pytest.mark.asyncio
async def test_a_failed_write_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(rule_usage, "async_session_factory", lambda: _Session(fail=True))

    # The caller is mid-way through handing a pack to an agent; telemetry must
    # not be the reason that fails.
    assert await rule_usage.record_rules_delivered(["a-rule"]) == 0


@pytest.mark.asyncio
async def test_composition_columns_are_added_without_defaults():
    connection = AsyncMock()

    await _ensure_context_pack_composition_columns(connection)

    statements = [str(call.args[0]) for call in connection.execute.await_args_list]
    for column in ("token_estimate", "file_count", "rule_count", "decision_count", "learning_count", "consumer"):
        assert any(f"ADD COLUMN IF NOT EXISTS {column}" in s for s in statements), column
    # A DEFAULT 0 would rewrite history: old packs would claim they delivered
    # nothing rather than admitting they were never measured.
    assert not any("DEFAULT" in s for s in statements)
    assert any("CREATE INDEX IF NOT EXISTS ix_context_packs_consumer" in s for s in statements)


@pytest.mark.asyncio
async def test_rule_usage_table_is_idempotent_and_window_indexed():
    connection = AsyncMock()

    await _ensure_rule_usage_events_table(connection)

    statements = [str(call.args[0]) for call in connection.execute.await_args_list]
    assert "CREATE TABLE IF NOT EXISTS rule_usage_events" in statements[0]
    assert any("ix_rule_usage_events_window" in s and "(rule_id, kind, created_at)" in s for s in statements)
