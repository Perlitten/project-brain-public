"""Persisted OAuth behavior, including SQLite expiry normalization and isolation."""
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from brain.auth import oauth_store
from brain.auth.principals import hash_credential, resolve_credential
from brain.database.models import ApiCredential, OAuthClient, OAuthRefreshToken, Principal
from brain.team.models import TeamAccessFence


@pytest_asyncio.fixture
async def oauth_storage(tmp_path):
    path = tmp_path / "oauth.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: Principal.metadata.create_all(
            sync, tables=[model.__table__ for model in (Principal, ApiCredential, OAuthClient, OAuthRefreshToken,
                                                       TeamAccessFence)],
        ))
    try:
        yield async_sessionmaker(engine, expire_on_commit=False), path
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_client_updates_are_persisted_without_overwriting_another_client(oauth_storage):
    sessions, _ = oauth_storage
    async with sessions() as session, session.begin():
        await oauth_store.save_client(session, "reader", {"client_name": "Reader", "scope": "core:read"})
        await oauth_store.save_client(session, "writer", {"client_name": "Writer", "scope": "core:write"})
    async with sessions() as session, session.begin():
        await oauth_store.save_client(session, "reader", {"client_name": "Renamed", "scope": "jobs:read"})
    async with sessions() as session:
        assert await oauth_store.load_client(session, "reader") == {"client_name": "Renamed", "scope": "jobs:read"}
        assert await oauth_store.load_client(session, "writer") == {"client_name": "Writer", "scope": "core:write"}
        assert await oauth_store.load_client(session, "unknown") is None


@pytest.mark.asyncio
async def test_pair_rotation_preserves_attribution_and_revocation_stays_with_its_client(oauth_storage):
    sessions, path = oauth_storage
    async with sessions() as session, session.begin():
        for client_id in ("client-reader", "client-other"):
            await oauth_store.save_client(session, client_id, {"client_name": client_id})
        original = await oauth_store.issue_tokens(
            session, client_id="client-reader", client_name="Reader", scopes=["core:write", "jobs:read"],
            subject="approver:17", access_ttl=timedelta(hours=1), refresh_ttl=timedelta(days=1),
        )
        other = await oauth_store.issue_tokens(
            session, client_id="client-other", client_name=None, scopes=[], subject=None,
            access_ttl=timedelta(minutes=5), refresh_ttl=None,
        )
    # Both raw credentials must remain absent from the persisted database.
    stored = path.read_bytes()
    assert original.access_token.encode() not in stored
    assert original.refresh_token.encode() not in stored
    async with sessions() as session:
        access = await oauth_store.load_access(session, original.access_token)
        refresh = await oauth_store.load_refresh(session, original.refresh_token)
        assert access is not None and refresh is not None
        assert (access.token, access.client_id, access.scopes, access.expires_at) == (
            original.access_token, "client-reader", ["core:write", "jobs:read"], original.access_expires_at,
        )
        assert (refresh.token, refresh.client_id, refresh.subject, refresh.scopes) == (
            original.refresh_token, "client-reader", "approver:17", ["core:write", "jobs:read"],
        )
        assert refresh.expires_at is not None and refresh.expires_at > access.expires_at
        identity = await resolve_credential(session, original.access_token)
        assert identity is not None and identity.kind == "service"
        saved_refresh = (await session.execute(select(OAuthRefreshToken).where(
            OAuthRefreshToken.client_id == "client-reader",
        ))).scalar_one()
        assert saved_refresh.principal_name == identity.name
        unlimited = await oauth_store.load_refresh(session, other.refresh_token)
        assert unlimited is not None and unlimited.expires_at is None and unlimited.scopes == []
    rotation_started = datetime.now(timezone.utc)
    async with sessions() as session, session.begin():
        rotated = await oauth_store.rotate_refresh(
            session, original.refresh_token, client_name="Reader", scopes=["core:read"],
            access_ttl=timedelta(minutes=10), refresh_ttl=timedelta(days=2),
        )
        assert rotated is not None
    async with sessions() as session, session.begin():
        assert await oauth_store.load_refresh(session, original.refresh_token) is None
        assert await oauth_store.load_access(session, original.access_token) is None
        refreshed = await oauth_store.load_refresh(session, rotated.refresh_token)
        assert refreshed is not None
        assert (refreshed.client_id, refreshed.subject, refreshed.scopes) == ("client-reader", "approver:17", ["core:read"])
        assert refreshed.expires_at is not None
        assert rotation_started + timedelta(days=2) <= refreshed.expires_at <= datetime.now(timezone.utc) + timedelta(days=2)
        refreshed_identity = await resolve_credential(session, rotated.access_token)
        assert refreshed_identity is not None and refreshed_identity.id == identity.id
        assert (await oauth_store.load_access(session, rotated.access_token)).scopes == ["core:read"]
        await oauth_store.revoke(session, rotated.access_token)
    async with sessions() as session, session.begin():
        assert await oauth_store.load_refresh(session, rotated.refresh_token) is None
        assert await oauth_store.load_access(session, rotated.access_token) is None
        await oauth_store.revoke(session, "unknown-token")
        assert await oauth_store.load_access(session, other.access_token) is not None
        assert await oauth_store.load_refresh(session, other.refresh_token) is not None


@pytest.mark.asyncio
async def test_access_and_refresh_expire_independently_at_their_exact_deadlines(oauth_storage, monkeypatch):
    sessions, _ = oauth_storage
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(oauth_store, "datetime", Clock)
    async with sessions() as session, session.begin():
        await oauth_store.save_client(session, "expiring", {"client_name": "Expiring"})
        pair = await oauth_store.issue_tokens(
            session, client_id="expiring", client_name="Expiring", scopes=["core:read"], subject=None,
            access_ttl=timedelta(seconds=1), refresh_ttl=timedelta(seconds=2),
        )
    async with sessions() as session:
        assert await oauth_store.load_access(session, pair.access_token) is not None
        assert await oauth_store.load_refresh(session, pair.refresh_token) is not None
    now += timedelta(seconds=1)
    async with sessions() as session:
        assert await oauth_store.load_access(session, pair.access_token) is None
        assert await oauth_store.load_refresh(session, pair.refresh_token) is not None
    now += timedelta(seconds=1)
    async with sessions() as session, session.begin():
        assert await oauth_store.load_refresh(session, pair.refresh_token) is None
        assert await oauth_store.rotate_refresh(
            session, pair.refresh_token, client_name="Expiring", scopes=["core:read"],
            access_ttl=timedelta(hours=1), refresh_ttl=None,
        ) is None


@pytest.mark.asyncio
async def test_paired_revocation_preserves_the_first_recorded_revocation_times(oauth_storage, monkeypatch):
    sessions, _ = oauth_storage
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(oauth_store, "datetime", Clock)
    async with sessions() as session, session.begin():
        await oauth_store.save_client(session, "audit", {"client_name": "Audit"})
        pair = await oauth_store.issue_tokens(
            session, client_id="audit", client_name="Audit", scopes=[], subject=None,
            access_ttl=timedelta(hours=3), refresh_ttl=timedelta(days=1),
        )
    # An administrator may have revoked the access key before its refresh pair.
    async with sessions() as session, session.begin():
        access = (await session.execute(select(ApiCredential).where(
            ApiCredential.key_hash == hash_credential(pair.access_token),
        ))).scalar_one()
        access.revoked_at = now
        access_id = access.id
    access_revoked_at = now
    now += timedelta(hours=1)
    refresh_revoked_at = now
    async with sessions() as session, session.begin():
        await oauth_store.revoke(session, pair.refresh_token)
    now += timedelta(hours=1)
    async with sessions() as session, session.begin():
        await oauth_store.revoke(session, pair.access_token)
        await oauth_store.revoke(session, pair.refresh_token)
    async with sessions() as session:
        access = await session.get(ApiCredential, access_id)
        refresh = (await session.execute(select(OAuthRefreshToken).where(
            OAuthRefreshToken.client_id == "audit",
        ))).scalar_one()
        assert access.revoked_at == access_revoked_at
        assert refresh.revoked_at == refresh_revoked_at
        assert await oauth_store.load_access(session, pair.access_token) is None
        assert await oauth_store.load_refresh(session, pair.refresh_token) is None
