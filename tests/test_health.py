import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient

with (
    patch("sqlalchemy.ext.asyncio.create_async_engine"),
    patch("redis.asyncio.from_url"),
    patch("neo4j.AsyncGraphDatabase.driver"),
):
    from apps.api.main import app

client = TestClient(app)


@pytest.mark.asyncio
@patch("apps.api.routers.core.check_health")
async def test_health_endpoint_healthy(mock_check_health):
    mock_check_health.return_value = {
        "postgres": {"status": "healthy", "message": "SQLAlchemy async engine connection successful"},
        "redis": {"status": "healthy", "message": "Redis ping successful"},
        "neo4j": {"status": "healthy", "message": "Neo4j connection verified"},
    }

    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["details"]["postgres"]["status"] == "healthy"
    assert data["details"]["redis"]["status"] == "healthy"
    assert data["details"]["neo4j"]["status"] == "healthy"


@pytest.mark.asyncio
@patch("apps.api.routers.core.check_health")
async def test_health_endpoint_unhealthy(mock_check_health):
    mock_check_health.return_value = {
        "postgres": {"status": "healthy", "message": "SQLAlchemy async engine connection successful"},
        "redis": {"status": "unhealthy", "error": "Connection refused"},
        "neo4j": {"status": "healthy", "message": "Neo4j connection verified"},
    }

    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "error"
    assert data["details"]["postgres"]["status"] == "healthy"
    assert data["details"]["redis"]["status"] == "unhealthy"
    assert data["details"]["neo4j"]["status"] == "healthy"


@pytest.mark.asyncio
@patch(
    "apps.api.routers.core.redis_client.get",
    new_callable=AsyncMock,
)
@patch("apps.api.routers.core.build_info")
@patch("apps.api.routers.core.check_health")
async def test_ready_requires_release_worker_and_alert_channel(
    mock_check_health, mock_build_info, mock_worker_heartbeat
):
    mock_check_health.return_value = {
        "postgres": {"status": "healthy"},
        "redis": {"status": "healthy"},
        "neo4j": {"status": "healthy"},
    }
    mock_build_info.return_value = {
        "build_sha": "a" * 40,
        "source_digest": "b" * 64,
    }
    mock_worker_heartbeat.return_value = '{"at":"now"}'
    with (
        patch("apps.api.routers.core.settings.ENVIRONMENT", "production"),
        patch("apps.api.routers.core.settings.SELF_DIAGNOSIS_ENABLED", True),
        patch("apps.api.routers.core.settings.SELF_DIAGNOSIS_USE_LLM", True),
        patch("apps.api.routers.core.settings.TELEGRAM_ALERTS_ENABLED", True),
        patch(
            "apps.api.routers.core.settings.TELEGRAM_ALERT_BOT_TOKEN",
            "configured",
        ),
        patch(
            "apps.api.routers.core.settings.TELEGRAM_ALERT_CHAT_ID",
            "configured",
        ),
        patch("apps.api.routers.core.settings.DEFAULT_LLM_PROVIDER", "nvidia"),
        patch("apps.api.routers.core.settings.NVIDIA_API_KEY", "configured"),
    ):
        response = client.get("/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert all(response.json()["checks"].values())


@pytest.mark.asyncio
@patch(
    "apps.api.routers.core.redis_client.get",
    new_callable=AsyncMock,
    return_value=None,
)
@patch(
    "apps.api.routers.core.build_info",
    return_value={"build_sha": "unknown", "source_digest": "unknown"},
)
@patch("apps.api.routers.core.check_health")
async def test_ready_fails_closed_on_unknown_build_and_missing_worker(
    mock_check_health, _mock_build_info, _mock_worker_heartbeat
):
    mock_check_health.return_value = {
        "postgres": {"status": "healthy"},
        "redis": {"status": "healthy"},
        "neo4j": {"status": "healthy"},
    }
    with patch("apps.api.routers.core.settings.ENVIRONMENT", "production"):
        response = client.get("/ready")

    assert response.status_code == 503
    checks = response.json()["checks"]
    assert checks["release_identity"] is False
    assert checks["worker_heartbeat"] is False


@pytest.mark.asyncio
@patch("apps.api.routers.core.redis_client.get", new_callable=AsyncMock, return_value="alive")
@patch(
    "apps.api.routers.core.build_info",
    return_value={"build_sha": "a" * 40, "source_digest": "b" * 64},
)
@patch("apps.api.routers.core.check_health")
async def test_ready_accepts_deterministic_only_self_diagnosis(
    mock_check_health, _mock_build_info, _mock_worker_heartbeat
):
    mock_check_health.return_value = {
        "postgres": {"status": "healthy"},
        "redis": {"status": "healthy"},
        "neo4j": {"status": "healthy"},
    }
    with (
        patch("apps.api.routers.core.settings.ENVIRONMENT", "production"),
        patch("apps.api.routers.core.settings.SELF_DIAGNOSIS_ENABLED", True),
        patch("apps.api.routers.core.settings.SELF_DIAGNOSIS_USE_LLM", False),
        patch("apps.api.routers.core.settings.TELEGRAM_ALERTS_ENABLED", True),
        patch("apps.api.routers.core.settings.TELEGRAM_ALERT_BOT_TOKEN", "configured"),
        patch("apps.api.routers.core.settings.TELEGRAM_ALERT_CHAT_ID", "configured"),
    ):
        response = client.get("/ready")

    assert response.status_code == 200


@patch("apps.api.routers.core.LfmColbertProvider.aclose", new_callable=AsyncMock)
@patch(
    "apps.api.routers.core.LfmColbertProvider.health",
    new_callable=AsyncMock,
    side_effect=RuntimeError("sidecar down"),
)
@patch("apps.api.routers.core.check_health")
def test_ready_reports_optional_late_interaction_failure_without_draining_api(
    mock_check_health, _mock_late_health, _mock_late_close
):
    mock_check_health.return_value = {
        "postgres": {"status": "healthy"},
        "redis": {"status": "healthy"},
        "neo4j": {"status": "healthy"},
    }
    with (
        patch("apps.api.routers.core.settings.ENVIRONMENT", "local"),
        patch("apps.api.routers.core.settings.LATE_INTERACTION_RERANK_ENABLED", True),
    ):
        response = client.get("/ready")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    assert "late_interaction_provider" not in payload["checks"]
    assert payload["optional_dependencies"]["late_interaction_provider"] == "unhealthy"


@patch("apps.api.routers.core.check_health")
def test_ready_probes_remote_late_interaction_without_draining_api(
    mock_check_health,
):
    mock_check_health.return_value = {
        "postgres": {"status": "healthy"},
        "redis": {"status": "healthy"},
        "neo4j": {"status": "healthy"},
    }
    remote = MagicMock()
    remote.ready = AsyncMock(
        return_value=SimpleNamespace(status="failed_open"),
    )
    with (
        patch("apps.api.routers.core.settings.ENVIRONMENT", "local"),
        patch("apps.api.routers.core.settings.LATE_INTERACTION_RERANK_ENABLED", True),
        patch("apps.api.routers.core.settings.LATE_INTERACTION_REMOTE_ENABLED", True),
        patch(
            "apps.api.routers.core.get_late_interaction_client",
            return_value=remote,
        ),
    ):
        response = client.get("/ready")

    assert response.status_code == 200
    assert response.json()["optional_dependencies"]["late_interaction_provider"] == "unhealthy"
    remote.ready.assert_awaited_once()
