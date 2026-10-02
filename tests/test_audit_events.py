"""Audit events: the middleware records every mutating API call — allowed or

denied — with the resolved principal, into the append-only audit_events
table."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

with (
    patch("sqlalchemy.ext.asyncio.create_async_engine"),
    patch("redis.asyncio.from_url"),
    patch("neo4j.AsyncGraphDatabase.driver"),
):
    from apps.api.main import app

from fastapi.testclient import TestClient  # noqa: E402

from brain.audit.events import outcome_for_status, write_audit_event  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def mock_queue():
    queue = MagicMock()
    queue.enqueue = AsyncMock(return_value="job-1")
    with patch("apps.api.routers.jobs.JobQueue", return_value=queue):
        yield queue


@pytest.fixture
def audit_spy():
    with patch(
        "apps.api.audit_middleware.record_audit_event", new=AsyncMock()
    ) as spy:
        yield spy


def test_outcome_mapping():
    assert outcome_for_status(200) == "allowed"
    assert outcome_for_status(201) == "allowed"
    assert outcome_for_status(401) == "denied"
    assert outcome_for_status(403) == "denied"
    assert outcome_for_status(500) == "error"


def test_mutation_is_recorded_with_principal(mock_queue, audit_spy):
    response = client.post("/jobs/reindex", json={"clean": False})
    assert response.status_code == 200
    spy = audit_spy
    assert spy.await_count == 1
    kwargs = spy.await_args.kwargs
    assert kwargs["method"] == "POST"
    assert kwargs["path"] == "/jobs/reindex"
    assert kwargs["status_code"] == 200
    # Dev mode resolves the synthetic open principal.
    assert kwargs["principal_name"] == "dev-open"
    assert kwargs["principal_id"] is None


class _SessionContext:
    def __init__(self):
        session = MagicMock()
        begin = MagicMock()
        begin.__aenter__ = AsyncMock(return_value=session)
        begin.__aexit__ = AsyncMock(return_value=False)
        session.begin = MagicMock(return_value=begin)
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *args):
        return False


def test_denied_attempt_is_recorded(mock_queue, audit_spy):
    """A failed authorization still produces an audit row."""
    with (
        patch("apps.api.auth.async_session_factory", return_value=_SessionContext()),
        patch("apps.api.auth.resolve_credential", AsyncMock(return_value=None)),
    ):
        response = client.post("/jobs/reindex", json={"clean": False}, headers={"X-API-Key": "pbk_bad"})
    assert response.status_code == 401
    assert audit_spy.await_count == 1
    kwargs = audit_spy.await_args.kwargs
    assert kwargs["status_code"] == 401
    assert kwargs["principal_name"] == "unresolved"


def test_reads_are_not_recorded(audit_spy):
    client.get("/health")
    client.get("/")
    client.get("/dashboard")
    assert audit_spy.await_count == 0


@pytest.mark.asyncio
async def test_write_audit_event_derives_outcome():
    session = MagicMock()
    session.add = MagicMock()
    event = await write_audit_event(
        session,
        principal_name="bot",
        principal_id=3,
        credential_id=9,
        method="POST",
        path="/jobs/reindex",
        status_code=403,
    )
    assert event.outcome == "denied"
    assert session.add.called
    assert event.principal_name == "bot"


def test_request_id_echoed_and_carried_into_audit(mock_queue, audit_spy):
    response = client.post(
        "/jobs/reindex", json={"clean": False}, headers={"X-Request-ID": "req-42"}
    )
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "req-42"
    assert audit_spy.await_args.kwargs["request_id"] == "req-42"


def test_request_id_generated_and_sanitized(mock_queue, audit_spy):
    response = client.post("/jobs/reindex", json={"clean": False})
    generated = response.headers["X-Request-ID"]
    assert len(generated) == 32
    assert audit_spy.await_args.kwargs["request_id"] == generated

    response = client.post(
        "/jobs/reindex",
        json={"clean": False},
        headers={"X-Request-ID": "a" * 200 + "<script>"},
    )
    # Malformed caller ids are replaced, not echoed.
    assert response.headers["X-Request-ID"] != "a" * 200 + "<script>"
    assert len(response.headers["X-Request-ID"]) == 32
