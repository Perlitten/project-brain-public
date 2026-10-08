from datetime import datetime, timezone
from datetime import timedelta

import pytest

from apps.api import telemetry


class _Result:
    def __init__(self, scalar_value=None, rows=()):
        self.scalar_value = scalar_value
        self.rows = rows

    def scalar(self):
        return self.scalar_value

    def all(self):
        return list(self.rows)

    def first(self):
        return self.rows[0] if self.rows else None


class _Session:
    def __init__(self, rows, collector_started=None):
        self.rows = rows
        self.collector_started = collector_started or datetime.now(timezone.utc) - timedelta(hours=2)
        self.statements = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def execute(self, statement):
        self.statements.append(statement)
        if len(self.statements) == 1:
            return _Result(rows=[(self.collector_started,)])
        # dashboard_metrics issues count then detail query.
        return _Result(len(self.rows) if len(self.statements) == 2 else None, self.rows)


@pytest.mark.asyncio
async def test_dashboard_metrics_filters_repository_and_computes_percentiles(monkeypatch):
    now = datetime.now(timezone.utc)
    rows = [
        ("search", "success", 10.0, now, "alice", 7, "r1", "http"),
        ("search", "error", 90.0, now, "bob", 7, "r2", "http"),
        ("context", "success", 30.0, now, "alice", 7, "r3", "mcp"),
    ]
    session = _Session(rows)
    monkeypatch.setattr(telemetry, "async_session_factory", lambda: session)

    result = await telemetry.dashboard_metrics(period_days=1, repository_id=7)

    assert result["requests"]["total"] == 3
    assert result["requests"]["failures"] == 1
    assert result["requests"]["clients"] == 2
    assert result["search"]["latency_ms"]["p95"] == 90.0
    assert result["context"]["successes"] == 1
    actual = [item for item in result["series"] if item["requests"]]
    assert actual[-1]["p95_ms"] == 90.0
    # Scope is part of the SQL statement, rather than a post-filter of returned rows.
    assert any("repository_id" in str(statement) for statement in session.statements)


@pytest.mark.asyncio
async def test_empty_period_is_measured_but_has_unknown_latency(monkeypatch):
    session = _Session([])
    monkeypatch.setattr(telemetry, "async_session_factory", lambda: session)

    result = await telemetry.dashboard_metrics(period_days=7)

    assert result["collection"]["status"] == "measured"
    assert result["requests"]["total"] == 0
    assert result["requests"]["latency_ms"] == {"p50": None, "p95": None}
    assert result["series"]
    assert any(item["requests"] == 0 for item in result["series"])


@pytest.mark.asyncio
async def test_record_request_does_not_break_call_when_telemetry_store_fails(monkeypatch):
    class Broken:
        async def __aenter__(self):
            raise RuntimeError("database unavailable")

        async def __aexit__(self, *_):
            return False

    monkeypatch.setattr(telemetry, "async_session_factory", Broken)
    await telemetry.record_request(
        operation="search", repository_id=None, repository_path=None,
        principal_name="test", request_id="req-1", outcome="error", latency_ms=4,
    )


@pytest.mark.asyncio
async def test_daily_buckets_keep_non_midnight_events_and_unmeasured_gaps(monkeypatch):
    now = datetime.now(timezone.utc)
    recorded = now - timedelta(hours=1)
    collector_started = now - timedelta(days=2)
    session = _Session([("search", "success", 65.0, recorded, "mcp:unattributed", 7, "r1", "mcp")], collector_started)
    monkeypatch.setattr(telemetry, "async_session_factory", lambda: session)
    result = await telemetry.dashboard_metrics(period_days=7)
    series = result["series"]
    assert len(series) == 8
    assert sum(item["requests"] or 0 for item in series) == 1
    assert any(item["requests"] is None for item in series)
    assert any(item["requests"] == 0 for item in series)
    assert result["collection"]["collection_started_at"] == collector_started.isoformat()
    assert result["collection"]["partial"] is True
    assert result["requests"]["clients"] == 0
    assert result["requests"]["unattributed"] == 1
    assert result["recent_requests"][0]["surface"] == "mcp"


@pytest.mark.asyncio
async def test_partial_and_empty_responses_do_not_count_as_delivery_or_failure(monkeypatch):
    now = datetime.now(timezone.utc)
    session = _Session([( "context", outcome, 10, now, "alice", 7, f"r{i}", "http")
                        for i, outcome in enumerate(("success", "partial", "empty", "cancelled"))])
    monkeypatch.setattr(telemetry, "async_session_factory", lambda: session)
    result = await telemetry.dashboard_metrics(period_days=1)
    assert result["context"]["total"] == 4
    assert result["context"]["successes"] == 1
    assert result["context"]["failures"] == 1
    assert result["context"]["partial"] == 1
    assert result["context"]["empty"] == 1
