"""Authentication decisions against persisted credentials, not a key-blind fake."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI, Request
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from apps.api import auth
from brain.auth import principals
from brain.database.models import ApiCredential, Base, Principal


@pytest_asyncio.fixture
async def credential_store(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'auth.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=[Principal.__table__, ApiCredential.__table__]))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(auth, "async_session_factory", factory)
    monkeypatch.setattr(auth.settings, "PROJECT_BRAIN_API_KEY", "test-legacy-key")
    monkeypatch.setattr(auth.settings, "ENVIRONMENT", "production")
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_persisted_lookup_distinguishes_keys_scopes_and_revocation(credential_store, monkeypatch):
    factory = credential_store
    async with factory() as session, session.begin():
        reader, read_credential = await principals.mint_credential(session, name="reader", scopes=["jobs:read"])
        writer, write_credential = await principals.mint_credential(session, name="writer", scopes=["jobs:write"])
        empty, _ = await principals.mint_credential(session, name="empty", scopes=[])
        await session.flush()
        read_id = read_credential.id
        assert read_credential.key_hash != write_credential.key_hash
        assert reader not in (read_credential.key_hash, write_credential.key_hash)

    app = FastAPI()

    @app.get("/read", dependencies=[Depends(auth.require_scope("jobs:read"))])
    async def read(request: Request):
        return {"name": request.state.principal.name}

    @app.post("/write", dependencies=[Depends(auth.require_scope("jobs:write"))])
    async def write():
        return {"ok": True}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        assert (await client.get("/read", headers={"X-API-Key": reader})).json() == {"name": "reader"}
        assert (await client.post("/write", headers={"X-API-Key": reader})).status_code == 403
        assert (await client.get("/read", headers={"X-API-Key": writer})).json() == {"name": "writer"}
        assert (await client.post("/write", headers={"X-API-Key": writer})).status_code == 200
        assert (await client.get("/read", headers={"X-API-Key": empty})).status_code == 403
        assert (await client.get("/read", headers={"X-API-Key": "pbk_unknown"})).status_code == 401
        assert (await client.get("/read")).status_code == 401
        assert (await client.get("/read", headers={"X-API-Key": "test-legacy-key"})).json() == {"name": "legacy-api-key"}
        with monkeypatch.context() as no_legacy:
            no_legacy.setattr(auth.settings, "PROJECT_BRAIN_API_KEY", "")
            assert (await client.get("/read", headers={"X-API-Key": writer})).json() == {"name": "writer"}
            assert (await client.get("/read")).status_code == 503
        async with factory() as session, session.begin():
            credential = await session.get(ApiCredential, read_id)
            credential.revoked_at = datetime.now(timezone.utc)
        assert (await client.get("/read", headers={"X-API-Key": reader})).status_code == 401


@pytest.mark.asyncio
async def test_expiry_boundary_and_disabled_principal_survive_storage_roundtrip(credential_store, monkeypatch):
    factory = credential_store
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(principals, "datetime", Clock)
    async with factory() as session, session.begin():
        expired, cred = await principals.mint_credential(session, name="expired", scopes=["jobs:read"])
        cred.expires_at = now
        active, cred2 = await principals.mint_credential(session, name="active", scopes=["jobs:read"])
        cred2.expires_at = now + timedelta(microseconds=1)
        await session.flush()
        active_principal = await session.get(Principal, cred2.principal_id)
        active_id = active_principal.id
    assert await auth.resolve_api_key(expired) is None
    assert (await auth.resolve_api_key(active)).name == "active"
    async with factory() as session, session.begin():
        principal = await session.get(Principal, active_id)
        principal.disabled_at = now
    assert await auth.resolve_api_key(active) is None


@pytest.mark.asyncio
async def test_mint_reuses_only_matching_principal_and_sets_future_expiry(credential_store):
    factory = credential_store
    before = datetime.now(timezone.utc)
    async with factory() as session, session.begin():
        _, first = await principals.mint_credential(session, name="first", scopes=["jobs:read"], ttl_days=2)
        await session.flush()
        _, second = await principals.mint_credential(session, name="second", scopes=[])
        _, reused = await principals.mint_credential(session, name="first", scopes=["jobs:write"])
        assert first.principal_id != second.principal_id
        assert reused.principal_id == first.principal_id
        assert before + timedelta(days=2) <= first.expires_at <= datetime.now(timezone.utc) + timedelta(days=2)
        assert second.expires_at is None


@pytest.mark.parametrize("environment,key,opened,status", [
    ("production", "", False, 503), ("PRODUCTION", "", False, 503),
    ("development", "", True, None), ("test", "", True, None),
    ("production", "configured", False, 401), ("test", "configured", False, 401),
])
@pytest.mark.asyncio
async def test_keyless_modes_fail_closed_in_production(monkeypatch, environment, key, opened, status):
    monkeypatch.setattr(auth.settings, "ENVIRONMENT", environment)
    monkeypatch.setattr(auth.settings, "PROJECT_BRAIN_API_KEY", key)
    assert auth.dev_open() is opened
    request = Request({"type": "http"})
    if status is None:
        assert await auth.require_principal(request, None) is auth.DEV_PRINCIPAL
    else:
        with pytest.raises(HTTPException) as raised:
            await auth.require_principal(request, None)
        assert raised.value.status_code == status


@pytest.mark.parametrize("grants,requested,expected", [
    ({"jobs:write"}, "jobs:read", True), ({"jobs:read"}, "jobs:write", False),
    ({"jobs:write"}, "memory:read", False), (set(), "jobs:read", False),
    ({"*"}, "memory:write", True), ({"jobs:write"}, "jobs", False),
])
def test_scope_grants_have_exact_domain_and_direction(grants, requested, expected):
    principal = principals.ResolvedPrincipal(7, "scoped", "service", frozenset(grants))
    assert principal.has_scope(requested) is expected
