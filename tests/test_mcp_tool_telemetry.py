"""Every MCP tools/call is timed and its outcome recorded, without touching the call."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from apps.mcp_server import client_activity as ca


def test_call_failed_reads_sdk_flag_and_brain_payloads():
    assert ca._call_failed(SimpleNamespace(is_error=True))
    assert ca._call_failed(SimpleNamespace(is_error=False, structured_content={"status": "failed", "error": "x"}))
    assert ca._call_failed(SimpleNamespace(is_error=False, structured_content={"result": {"error": "boom"}}))
    assert not ca._call_failed(SimpleNamespace(is_error=False, structured_content={"status": "ok"}))
    assert not ca._call_failed(SimpleNamespace(is_error=False, structured_content=None))


@pytest.mark.asyncio
async def test_observer_records_tool_calls_only():
    recorded = []
    observe = ca.tool_call_observer("stdio")

    async def ok(_ctx):
        return SimpleNamespace(is_error=False, structured_content={"status": "ok"})

    with patch.object(ca, "_record_tool_call", lambda *args: recorded.append(args)):
        await observe(SimpleNamespace(method="tools/call", params={"name": "search_code"}), ok)
        await observe(SimpleNamespace(method="tools/list", params={}), ok)

    assert len(recorded) == 1
    tool, surface, outcome, latency = recorded[0]
    assert (tool, surface, outcome) == ("search_code", "stdio", "success")
    assert latency >= 0


@pytest.mark.asyncio
async def test_observer_records_errors_and_respects_enabled_gate():
    recorded = []

    async def boom(_ctx):
        raise RuntimeError("tool crashed")

    with patch.object(ca, "_record_tool_call", lambda *args: recorded.append(args)):
        with pytest.raises(RuntimeError):
            await ca.tool_call_observer("http")(SimpleNamespace(method="tools/call", params={"name": "ask_project"}), boom)
        off = ca.tool_call_observer("http", enabled=lambda: False)

        async def ok(_ctx):
            return SimpleNamespace(is_error=False)

        await off(SimpleNamespace(method="tools/call", params={"name": "ask_project"}), ok)

    assert recorded == [("ask_project", "http", "error", recorded[0][3])]


@pytest.mark.asyncio
async def test_summary_works_on_sqlite_lite_mode():
    """Lite mode runs on SQLite: the summary must not rely on Postgres-only aggregates."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from brain.database.models import McpToolCall
    from brain.telemetry import tool_calls

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(McpToolCall.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    with patch.object(tool_calls, "async_session_factory", factory):
        for latency, outcome in ((10, "success"), (20, "success"), (30, "error"), (400, "success")):
            await tool_calls.record_tool_call("search_code", "stdio", outcome, latency)
        await tool_calls.record_tool_call("record_decision", "http", "success", 5)
        summary = await tool_calls.summarize_tool_calls()
    await engine.dispose()

    assert summary["search_code"] == {"calls_24h": 4, "errors_24h": 1, "error_rate": 0.25, "p50_ms": 20.0, "p95_ms": 400.0}
    assert summary["record_decision"]["calls_24h"] == 1
