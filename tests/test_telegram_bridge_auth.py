"""Telegram bridge auth: legacy shared key still works; scoped ``pbk_``

credentials resolve via Bearer or X-API-Key and must carry ``bridge:write``;
the resolved principal is placed on request.state for audit attribution."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch


with (
    patch("sqlalchemy.ext.asyncio.create_async_engine"),
    patch("redis.asyncio.from_url"),
    patch("neo4j.AsyncGraphDatabase.driver"),
):
    from apps.api.main import app

from fastapi.testclient import TestClient  # noqa: E402

from brain.auth.principals import ResolvedPrincipal  # noqa: E402
from brain.config.settings import settings  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)

BRIDGE_PRINCIPAL = ResolvedPrincipal(
    id=9,
    name="telegram-poller",
    kind="service",
    scopes=frozenset({"bridge:write"}),
    credential_id=41,
)


class _NullSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def begin(self):
        return self


def _capture(**kwargs):
    return client.post("/v1/intake/capture", json={}, **kwargs)


def test_legacy_shared_key_via_bearer_still_works():
    with patch.object(settings, "PROJECT_BRAIN_API_KEY", "shared-secret"):
        response = _capture(headers={"Authorization": "Bearer shared-secret"})
    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}


def test_scoped_credential_via_bearer_resolves():
    with (
        patch.object(settings, "PROJECT_BRAIN_API_KEY", "shared-secret"),
        patch(
            "apps.api.routers.telegram_bridge.async_session_factory",
            lambda: _NullSession(),
        ),
        patch(
            "apps.api.routers.telegram_bridge.resolve_credential",
            new=AsyncMock(return_value=BRIDGE_PRINCIPAL),
        ) as resolve,
    ):
        response = _capture(headers={"Authorization": "Bearer pbk_poller"})
    assert response.status_code == 200
    resolve.assert_awaited_once()


def test_scoped_credential_via_x_api_key_resolves():
    with (
        patch.object(settings, "PROJECT_BRAIN_API_KEY", "shared-secret"),
        patch(
            "apps.api.routers.telegram_bridge.async_session_factory",
            lambda: _NullSession(),
        ),
        patch(
            "apps.api.routers.telegram_bridge.resolve_credential",
            new=AsyncMock(return_value=BRIDGE_PRINCIPAL),
        ),
    ):
        response = _capture(headers={"X-API-Key": "pbk_poller"})
    assert response.status_code == 200


def test_credential_without_bridge_scope_denied():
    narrow = ResolvedPrincipal(
        id=9,
        name="telegram-poller",
        kind="service",
        scopes=frozenset({"jobs:read"}),
    )
    with (
        patch.object(settings, "PROJECT_BRAIN_API_KEY", "shared-secret"),
        patch(
            "apps.api.routers.telegram_bridge.async_session_factory",
            lambda: _NullSession(),
        ),
        patch(
            "apps.api.routers.telegram_bridge.resolve_credential",
            new=AsyncMock(return_value=narrow),
        ),
    ):
        response = _capture(headers={"Authorization": "Bearer pbk_poller"})
    assert response.status_code == 403


def test_unresolvable_key_denied():
    with (
        patch.object(settings, "PROJECT_BRAIN_API_KEY", "shared-secret"),
        patch(
            "apps.api.routers.telegram_bridge.async_session_factory",
            lambda: _NullSession(),
        ),
        patch(
            "apps.api.routers.telegram_bridge.resolve_credential",
            new=AsyncMock(return_value=None),
        ),
    ):
        response = _capture(headers={"Authorization": "Bearer pbk_bogus"})
    assert response.status_code == 401


def test_missing_key_denied_when_auth_configured():
    with patch.object(settings, "PROJECT_BRAIN_API_KEY", "shared-secret"):
        response = _capture()
    assert response.status_code == 401
