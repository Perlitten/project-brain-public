"""Malformed credentials and lifetime values must fail closed."""
from unittest.mock import AsyncMock

import pytest

from apps.api import auth
from brain.auth.principals import mint_credential


@pytest.mark.asyncio
async def test_empty_key_never_resolves_to_legacy_admin(monkeypatch):
    monkeypatch.setattr(auth.settings, "PROJECT_BRAIN_API_KEY", "")
    monkeypatch.setattr(auth, "async_session_factory", lambda: pytest.fail("empty key must not query storage"))
    assert await auth.resolve_api_key("") is None


@pytest.mark.asyncio
async def test_unicode_keys_do_not_raise_in_constant_time_comparison(monkeypatch):
    monkeypatch.setattr(auth.settings, "PROJECT_BRAIN_API_KEY", "clé-test")
    assert await auth.resolve_api_key("clé-test") is auth.LEGACY_PRINCIPAL


@pytest.mark.parametrize("ttl", [0, -1])
@pytest.mark.asyncio
async def test_invalid_credential_lifetime_is_rejected_before_storage(ttl):
    session = AsyncMock()
    with pytest.raises(ValueError, match="ttl_days"):
        await mint_credential(session, name="invalid", scopes=["jobs:read"], ttl_days=ttl)
    session.execute.assert_not_called()
