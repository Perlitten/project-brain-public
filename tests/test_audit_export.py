"""Audit read/export endpoints: paged JSON over audit_events, CSV export,

and ``audit:read`` scope enforcement. DB access is faked — the tests pin the
route contract and the filter-to-SQL mapping, not Postgres."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch


with (
    patch("sqlalchemy.ext.asyncio.create_async_engine"),
    patch("redis.asyncio.from_url"),
    patch("neo4j.AsyncGraphDatabase.driver"),
):
    from apps.api.main import app

from fastapi.testclient import TestClient  # noqa: E402

from brain.auth.principals import ResolvedPrincipal  # noqa: E402
from brain.database.models import AuditEvent  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)


def _event(**overrides):
    base = {
        "id": 7,
        "created_at": datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc),
        "principal_name": "svc-ci",
        "principal_id": 3,
        "credential_id": 9,
        "method": "POST",
        "path": "/jobs/reindex",
        "status_code": 200,
        "outcome": "allowed",
        "request_id": "req-1",
    }
    base.update(overrides)
    return AuditEvent(**base)


class _FakeSession:
    """Captures the statement so tests can assert the compiled filters."""

    def __init__(self, rows, captured):
        self._rows = rows
        self._captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def scalars(self, stmt):
        self._captured.append(stmt)
        rows = self._rows

        class _Scalars:
            def all(self):
                return rows

        return _Scalars()


def _patch_session(rows):
    captured = []
    patcher = patch(
        "apps.api.routers.audit.async_session_factory",
        lambda: _FakeSession(rows, captured),
    )
    return patcher, captured


def test_list_events_returns_serialized_rows():
    patcher, _ = _patch_session([_event()])
    with patcher:
        response = client.get("/audit/events")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    event = body["events"][0]
    assert event["id"] == 7
    assert event["principal_name"] == "svc-ci"
    assert event["path"] == "/jobs/reindex"
    assert event["outcome"] == "allowed"
    assert event["request_id"] == "req-1"


def test_list_events_filters_compile_into_query():
    patcher, captured = _patch_session([])
    with patcher:
        response = client.get(
            "/audit/events",
            params={
                "outcome": "denied",
                "principal": "svc-ci",
                "path_prefix": "/jobs",
                "since": "2026-10-01T00:00:00Z",
                "until": "2026-10-03T00:00:00Z",
            },
        )
    assert response.status_code == 200
    sql = str(captured[0].compile())
    for fragment in ("outcome", "principal_name", "path LIKE", "created_at >="):
        assert fragment in sql, f"filter missing from compiled query: {fragment}"


def test_list_events_rejects_unknown_outcome():
    patcher, captured = _patch_session([])
    with patcher:
        response = client.get("/audit/events", params={"outcome": "suspicious"})
    assert response.status_code == 400
    assert captured == []


def test_list_events_limit_is_bounded():
    patcher, _ = _patch_session([])
    with patcher:
        response = client.get("/audit/events", params={"limit": 5000})
    assert response.status_code == 422


def test_export_streams_csv_with_header_and_rows():
    patcher, _ = _patch_session(
        [
            _event(path="/jobs/reindex,arg"),  # comma must be escaped by csv writer
            _event(id=8, request_id=None, outcome="denied", status_code=403),
        ]
    )
    with patcher:
        response = client.get("/audit/export")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers.get("content-disposition", "")
    lines = response.text.splitlines()
    assert lines[0] == (
        "id,created_at,principal_name,principal_id,credential_id,"
        "method,path,status_code,outcome,request_id"
    )
    assert len(lines) == 3
    assert '"/jobs/reindex,arg"' in lines[1]
    assert lines[2].endswith("denied,")


def test_export_limit_is_bounded():
    patcher, _ = _patch_session([])
    with patcher:
        response = client.get("/audit/export", params={"limit": 50000})
    assert response.status_code == 422


def test_audit_routes_require_audit_read_scope():
    principal = ResolvedPrincipal(
        id=5,
        name="limited-cred",
        kind="service",
        scopes=frozenset({"jobs:read"}),
    )
    with patch(
        "apps.api.auth.require_principal",
        new=AsyncMock(return_value=principal),
    ):
        response = client.get("/audit/events")
    assert response.status_code == 403
