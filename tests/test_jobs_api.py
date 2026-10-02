"""Tests for job trigger API endpoints (worker queue mocked)."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

with (
    patch("sqlalchemy.ext.asyncio.create_async_engine"),
    patch("redis.asyncio.from_url"),
    patch("neo4j.AsyncGraphDatabase.driver"),
):
    from apps.api.main import app

from fastapi.testclient import TestClient

client = TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def mock_queue():
    queue = MagicMock()
    queue.enqueue = AsyncMock(return_value="test-job-id-123")
    queue.get_job = AsyncMock(
        return_value={
            "id": "test-job-id-123",
            "type": "reindex",
            "status": "completed",
            "params": {},
            "result": {"repo_id": 1},
            "error": "",
            "created_at": "2026-06-26T00:00:00+00:00",
            "updated_at": "2026-06-26T00:01:00+00:00",
        }
    )
    with patch("apps.api.routers.jobs.JobQueue", return_value=queue):
        yield queue


@pytest.fixture
def api_key_env():
    with patch("apps.api.auth.settings") as mock_settings:
        mock_settings.PROJECT_BRAIN_API_KEY = "test-secret-key"
        yield mock_settings


def test_enqueue_reindex_requires_api_key_when_configured(mock_queue, api_key_env):
    response = client.post("/jobs/reindex", json={"clean": False})
    assert response.status_code == 401

    response = client.post(
        "/jobs/reindex",
        json={"clean": False},
        headers={"X-API-Key": "test-secret-key"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["job_id"] == "test-job-id-123"
    assert data["status"] == "queued"
    assert data["status_url"] == "/jobs/test-job-id-123"
    mock_queue.enqueue.assert_awaited_once_with(
        "reindex",
        {
            "clean": False,
            "verify_after": True,
            "benchmark_after": False,
        },
        idempotency_key=None,
        max_attempts=3,
    )


def test_enqueue_reindex_closed_loop_options(mock_queue, api_key_env):
    response = client.post(
        "/jobs/reindex",
        json={
            "repo_path": "/app",
            "clean": False,
            "source_revision": "abc123",
            "verify_after": True,
            "benchmark_after": True,
            "idempotency_key": "git:abc123",
        },
        headers={"X-API-Key": "test-secret-key"},
    )
    assert response.status_code == 200
    mock_queue.enqueue.assert_awaited_once_with(
        "reindex",
        {
            "repo_path": "/app",
            "clean": False,
            "source_revision": "abc123",
            "verify_after": True,
            "benchmark_after": True,
        },
        idempotency_key="git:abc123",
        max_attempts=3,
    )


def test_enqueue_health_check(mock_queue, api_key_env):
    response = client.post(
        "/jobs/health-check",
        headers={"X-API-Key": "test-secret-key"},
    )
    assert response.status_code == 200
    mock_queue.enqueue.assert_awaited_once_with("health_check", None)


def test_enqueue_benchmark_smoke(mock_queue, api_key_env):
    response = client.post(
        "/jobs/benchmark",
        json={"smoke": True},
        headers={"X-API-Key": "test-secret-key"},
    )
    assert response.status_code == 200
    mock_queue.enqueue.assert_awaited_once_with("benchmark", {"smoke": True})


def test_enqueue_proactive_insights(mock_queue, api_key_env):
    response = client.post(
        "/jobs/proactive-insights",
        json={"repo_path": "/tmp/repo", "use_llm": True},
        headers={"X-API-Key": "test-secret-key"},
    )
    assert response.status_code == 200
    mock_queue.enqueue.assert_awaited_once_with(
        "proactive_insights",
        {"repo_path": "/tmp/repo", "use_llm": True},
    )


def test_enqueue_scheduled_proactive_insights(mock_queue, api_key_env):
    response = client.post(
        "/jobs/proactive-insights",
        json={"scheduled": True},
        headers={"X-API-Key": "test-secret-key"},
    )
    assert response.status_code == 200
    mock_queue.enqueue.assert_awaited_once_with("proactive_insights", {"scheduled": True})


def test_enqueue_self_diagnosis_is_idempotent_and_retryable(mock_queue, api_key_env):
    with patch("apps.api.routers.jobs.settings.SELF_DIAGNOSIS_JOB_MAX_ATTEMPTS", 4):
        response = client.post(
            "/jobs/self-diagnosis",
            json={
                "scheduled": True,
                "use_llm": True,
                "idempotency_key": "nightly:2026-07-28",
            },
            headers={"X-API-Key": "test-secret-key"},
        )
    assert response.status_code == 200
    mock_queue.enqueue.assert_awaited_once_with(
        "self_diagnosis",
        {"use_llm": True, "scheduled": True},
        idempotency_key="nightly:2026-07-28",
        max_attempts=4,
    )


def test_enqueue_embedding_verify(mock_queue, api_key_env):
    response = client.post(
        "/jobs/embedding-verify",
        json={"repo_path": "/tmp/repo"},
        headers={"X-API-Key": "test-secret-key"},
    )
    assert response.status_code == 200
    mock_queue.enqueue.assert_awaited_once_with("embedding_verify", {"repo_path": "/tmp/repo"})


def test_enqueue_embedding_backfill(mock_queue, api_key_env):
    response = client.post(
        "/jobs/embedding-backfill",
        json={"repo_path": "/tmp/repo", "limit": 98, "batch_size": 4},
        headers={"X-API-Key": "test-secret-key"},
    )
    assert response.status_code == 200
    mock_queue.enqueue.assert_awaited_once_with(
        "embedding_backfill",
        {"repo_path": "/tmp/repo", "limit": 98, "batch_size": 4},
    )


def test_enqueue_nightly_maintenance_has_long_quality_budget(
    mock_queue,
    api_key_env,
):
    with patch(
        "apps.api.routers.jobs.settings.WORKER_MAX_JOB_TIMEOUT_SECONDS",
        21_600,
    ):
        response = client.post(
            "/jobs/nightly-maintenance",
            json={
                "repo_path": "/app",
                "scheduled": True,
                "notify": False,
                "idempotency_key": "nightly-deep:2026-07-30",
            },
            headers={"X-API-Key": "test-secret-key"},
        )

    assert response.status_code == 200
    mock_queue.enqueue.assert_awaited_once_with(
        "nightly_maintenance",
        {
            "repo_path": "/app",
            "scheduled": True,
            "notify": False,
        },
        idempotency_key="nightly-deep:2026-07-30",
        max_attempts=2,
        timeout_seconds=21_600,
    )


def test_enqueue_deep_context_is_authenticated_and_explicitly_gated(
    mock_queue,
    api_key_env,
):
    body = {
        "task_description": "trace durable LFM maintenance",
        "repo_path": "/app",
        "notify": False,
    }
    with patch(
        "apps.api.routers.jobs.settings.LATE_INTERACTION_DEEP_ENABLED",
        False,
    ):
        disabled = client.post(
            "/jobs/deep-context",
            json=body,
            headers={"X-API-Key": "test-secret-key"},
        )
    assert disabled.status_code == 503
    mock_queue.enqueue.assert_not_awaited()

    with (
        patch(
            "apps.api.routers.jobs.settings.LATE_INTERACTION_DEEP_ENABLED",
            True,
        ),
        patch(
            "apps.api.routers.jobs.settings.WORKER_MAX_JOB_TIMEOUT_SECONDS",
            21_600,
        ),
    ):
        enabled = client.post(
            "/jobs/deep-context",
            json=body,
            headers={"X-API-Key": "test-secret-key"},
        )

    assert enabled.status_code == 200
    mock_queue.enqueue.assert_awaited_once_with(
        "deep_context",
        body,
        idempotency_key=None,
        max_attempts=1,
        timeout_seconds=21_600,
    )


def test_get_job_status(mock_queue, api_key_env):
    response = client.get(
        "/jobs/test-job-id-123",
        headers={"X-API-Key": "test-secret-key"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    mock_queue.get_job.assert_awaited_once_with("test-job-id-123")


def test_get_job_status_does_not_claim_empty_queued_result_is_available(mock_queue, api_key_env):
    mock_queue.get_job = AsyncMock(
        return_value={
            "id": "test-job-id-123",
            "type": "deep_context",
            "status": "queued",
            "result": "",
        }
    )

    response = client.get(
        "/jobs/test-job-id-123?include_result=false",
        headers={"X-API-Key": "test-secret-key"},
    )

    assert response.status_code == 200
    assert "result" not in response.json()
    assert response.json()["result_available"] is False


def test_get_job_not_found(mock_queue, api_key_env):
    mock_queue.get_job = AsyncMock(return_value=None)
    response = client.get(
        "/jobs/missing-id",
        headers={"X-API-Key": "test-secret-key"},
    )
    assert response.status_code == 404


def test_no_auth_when_api_key_unset(mock_queue):
    with patch("apps.api.auth.settings") as mock_settings:
        mock_settings.PROJECT_BRAIN_API_KEY = None
        response = client.post("/jobs/reindex")
        assert response.status_code == 200


class _SessionContext:
    """async_session_factory() stand-in yielding a fake session whose

    session.begin() is also an async context manager."""

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


def _resolve_as(principal):
    return (
        patch("apps.api.auth.async_session_factory", return_value=_SessionContext()),
        patch("apps.api.auth.resolve_credential", AsyncMock(return_value=principal)),
    )


def test_minted_credential_enforces_scopes(mock_queue, api_key_env):
    from brain.auth.principals import ResolvedPrincipal

    principal = ResolvedPrincipal(
        id=7, name="indexer-bot", kind="service",
        scopes=frozenset({"jobs:read"}), credential_id=3,
    )
    with _resolve_as(principal)[0], _resolve_as(principal)[1]:
        headers = {"X-API-Key": "pbk_minted"}

        # Read scope does not cover job submission.
        response = client.post("/jobs/reindex", json={"clean": False}, headers=headers)
        assert response.status_code == 403
        assert "indexer-bot" in response.json()["detail"]

        # Read scope covers job status.
        response = client.get("/jobs/test-job-id-123", headers=headers)
        assert response.status_code == 200


def test_minted_credential_with_write_scope_can_enqueue(mock_queue, api_key_env):
    from brain.auth.principals import ResolvedPrincipal

    principal = ResolvedPrincipal(
        id=8, name="admin-bot", kind="service",
        scopes=frozenset({"jobs:write", "jobs:read"}),
    )
    cm, resolver = _resolve_as(principal)
    with cm, resolver:
        headers = {"X-API-Key": "pbk_admin"}
        response = client.post("/jobs/reindex", json={"clean": False}, headers=headers)
        assert response.status_code == 200


def test_unknown_minted_credential_is_denied(mock_queue, api_key_env):
    cm, resolver = _resolve_as(None)
    with cm, resolver:
        response = client.post(
            "/jobs/reindex", json={"clean": False}, headers={"X-API-Key": "pbk_bogus"}
        )
        assert response.status_code == 401
