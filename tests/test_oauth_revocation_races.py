"""Real PostgreSQL locking contracts; isolated OAuth-only schemas, no vector/model calls."""
import asyncio
import os
import secrets
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio.engine import create_async_engine

from apps.api.routers import admin
from brain.auth import oauth_store
from brain.auth.principals import ResolvedPrincipal, hash_credential, mint_credential, resolve_credential
from brain.config.settings import settings
from brain.database.models import ApiCredential, OAuthClient, OAuthRefreshToken, Principal
from brain.team import write_fence
from brain.team.authorization import TeamDenied
from brain.team.locking import sqlstate
from brain.team.models import Team, TeamAccessFence, TeamMember


@pytest_asyncio.fixture
async def race_storage():
    schema = f"oauth_race_{secrets.token_hex(8)}"
    url = os.environ.get("TEST_OAUTH_RACE_URL", settings.DATABASE_URL)
    if make_url(url).get_backend_name() != "postgresql":
        pytest.skip("Row-lock contracts require PostgreSQL, not the Lite SQLite profile")
    engine = create_async_engine(url,
                                 execution_options={"schema_translate_map": {None: schema}})
    created = False
    try:
        try:
            async with engine.begin() as connection:
                await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
                created = True
                await connection.run_sync(lambda sync: Principal.metadata.create_all(sync, tables=[
                    model.__table__ for model in (Principal, ApiCredential, OAuthClient, OAuthRefreshToken,
                                                  TeamAccessFence)]))
        except (OSError, ConnectionError):
            if os.environ.get("TEST_OAUTH_RACE_URL") or os.environ.get("GITHUB_ACTIONS") or os.environ.get(
                    "BRAIN_CI_REQUIRE_SERVICES"):
                raise
            pytest.skip("PostgreSQL unavailable; set TEST_OAUTH_RACE_URL to run real lock contracts")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions.begin() as session:
            await oauth_store.save_client(session, "race-client", {"client_name": "Race"})
            tokens = await oauth_store.issue_tokens(session, client_id="race-client", client_name="Race",
                scopes=["core:read"], subject=None, access_ttl=timedelta(hours=1), refresh_ttl=timedelta(days=1))
        async with sessions() as session:
            credential = (await session.scalars(select(ApiCredential).where(
                ApiCredential.key_hash == hash_credential(tokens.access_token)))).one()
        yield engine, sessions, tokens, credential
    finally:
        if created:
            async with engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await engine.dispose()


async def _wait_for_waiter(engine, holder_pid, pending):
    async def blocked():
        async with engine.connect() as observer:
            while not pending.done():
                if await observer.scalar(text(
                        "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE :holder = ANY(pg_blocking_pids(pid)))"
                ), {"holder": holder_pid}):
                    return
                await asyncio.sleep(.01)
            await pending
            pytest.fail("Expected a real PostgreSQL row-lock wait, not an uncontended operation")
    await asyncio.wait_for(blocked(), 5)


async def _rotate(session, token):
    return await oauth_store.rotate_refresh(session, token, client_name="Race", scopes=["core:read"],
        access_ttl=timedelta(hours=1), refresh_ttl=timedelta(days=1))


@pytest.mark.asyncio
@pytest.mark.parametrize("authority", ["credential", "principal"])
async def test_admin_winning_row_lock_prevents_refresh_issuance(race_storage, authority):
    engine, sessions, tokens, credential = race_storage
    async with sessions() as revoking, sessions() as rotating:
        await revoking.begin()
        holder_pid = await revoking.scalar(text("SELECT pg_backend_pid()"))
        if authority == "credential":
            await revoking.execute(update(ApiCredential).where(ApiCredential.id == credential.id)
                                   .values(revoked_at=datetime.now(timezone.utc)))
        else:
            await revoking.execute(update(Principal).where(Principal.id == credential.principal_id)
                                   .values(disabled_at=datetime.now(timezone.utc)))

        async def rotate_and_commit():
            async with rotating.begin():
                return await _rotate(rotating, tokens.refresh_token)

        rotation = asyncio.create_task(rotate_and_commit())
        try:
            await _wait_for_waiter(engine, holder_pid, rotation)
            if authority == "principal":
                # A blocked refresher must not hold later locks needed by principal revocation.
                await revoking.execute(select(ApiCredential).where(ApiCredential.id == credential.id)
                                       .with_for_update(nowait=True))
                await revoking.execute(select(OAuthRefreshToken).where(
                    OAuthRefreshToken.access_credential_id == credential.id).with_for_update(nowait=True))
            await revoking.commit()
            assert await asyncio.wait_for(rotation, 5) is None
        finally:
            rotation.cancel()
            await asyncio.gather(rotation, return_exceptions=True)
    async with sessions() as session:
        assert len((await session.scalars(select(ApiCredential))).all()) == 1
        assert len((await session.scalars(select(OAuthRefreshToken))).all()) == 1


@pytest.mark.asyncio
async def test_rotation_winning_lock_then_admin_revoke_ends_successor(race_storage, monkeypatch):
    engine, sessions, tokens, credential = race_storage
    monkeypatch.setattr(admin, "async_session_factory", sessions)

    async def solo():
        return False

    monkeypatch.setattr(admin, "_team_state_is_team", solo)
    caller = ResolvedPrincipal(id=0, name="fixture-admin", kind="service", scopes=frozenset({"*"}))
    async with sessions.begin() as rotating:
        holder_pid = await rotating.scalar(text("SELECT pg_backend_pid()"))
        issued = await _rotate(rotating, tokens.refresh_token)
        assert issued is not None
        revocation = asyncio.create_task(admin.revoke_credential(credential.id, caller))
        try:
            await _wait_for_waiter(engine, holder_pid, revocation)
            await rotating.commit()
            assert "credential" in await asyncio.wait_for(revocation, 5)
        finally:
            revocation.cancel()
            await asyncio.gather(revocation, return_exceptions=True)
    async with sessions() as session:
        assert await oauth_store.load_access(session, issued.access_token) is None
        assert await oauth_store.load_refresh(session, issued.refresh_token) is None
        assert all(row.revoked_at is not None for row in (await session.scalars(select(OAuthRefreshToken))).all())


@pytest.mark.asyncio
async def test_only_one_concurrent_refresh_can_claim_source(race_storage):
    _, sessions, tokens, _ = race_storage
    async def rotate():
        async with sessions.begin() as session:
            return await _rotate(session, tokens.refresh_token)
    first, second = await asyncio.wait_for(asyncio.gather(rotate(), rotate()), 5)
    assert sum(issued is not None for issued in (first, second)) == 1


@pytest.mark.asyncio
async def test_expired_access_still_refreshes_under_postgres_locks(race_storage):
    _, sessions, tokens, credential = race_storage
    async with sessions.begin() as session:
        await session.execute(update(ApiCredential).where(ApiCredential.id == credential.id)
                               .values(expires_at=datetime.now(timezone.utc) - timedelta(days=1)))
    async with sessions.begin() as session:
        issued = await _rotate(session, tokens.refresh_token)
        assert issued is not None
    async with sessions() as session:
        assert await oauth_store.load_access(session, issued.access_token) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["revoke", "disable"])
async def test_cross_admin_mutations_serialize_before_targets_with_real_fence(race_storage, monkeypatch, operation):
    engine, sessions, _, _ = race_storage
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: Team.metadata.create_all(
            sync, tables=[Team.__table__, TeamMember.__table__]))
    actors, keys = [], []
    async with sessions.begin() as session:
        session.add(Team(id=1, name="Cross-admin lock control"))
        await session.flush()
        for name in ("admin-a", "admin-b", "spare-admin"):
            raw, credential = await mint_credential(session, name=name, kind="human",
                                                    scopes=["team:admin", "principals:write"])
            await session.flush()
            session.add(TeamMember(principal_id=credential.principal_id, team_id=1,
                                   verification_issuer="fixture", verification_subject=name,
                                   verified_at=datetime.now(timezone.utc), is_admin=True))
            keys.append(raw)
            actors.append(ResolvedPrincipal(id=credential.principal_id, name=name, kind="human",
                credential_id=credential.id, scopes=frozenset(credential.scopes)))

    async with sessions() as session:
        assert set(await admin._live_admin_credentials(session)) == {
            (actor.id, actor.credential_id) for actor in actors}
    monkeypatch.setattr(admin, "async_session_factory", sessions)

    async def is_team():
        return True

    monkeypatch.setattr(admin, "_team_state_is_team", is_team)
    original = admin.fresh
    both_ready = asyncio.Event()
    arrivals = 0

    async def enter_team_gate(session, model, *predicates, **kwargs):
        nonlocal arrivals
        if model is Team:
            arrivals += 1
            if arrivals == 2:
                both_ready.set()
            # Synchronize before the actual serialization gate, not after it.
            await asyncio.wait_for(both_ready.wait(), 5)
        return await original(session, model, *predicates, **kwargs)

    monkeypatch.setattr(admin, "fresh", enter_team_gate)

    async def request(caller, target):
        with write_fence.request_scope():
            write_fence.arm(caller.id, caller.credential_id, caller.id, caller.credential_id)
            if operation == "revoke":
                return await admin.revoke_credential(target.credential_id, caller)
            return await admin.disable_principal(target.id, caller)

    results = await asyncio.wait_for(asyncio.gather(request(actors[0], actors[1]),
        request(actors[1], actors[0]), return_exceptions=True), 12)
    assert arrivals == 2
    assert all(sqlstate(result) != "40P01" for result in results if isinstance(result, BaseException)), results
    assert sum(isinstance(result, dict) for result in results) == 1, results
    assert sum(isinstance(result, TeamDenied) for result in results) == 1, results
    winner = next(index for index, result in enumerate(results) if isinstance(result, dict))
    loser = 1 - winner
    if operation == "revoke":
        assert results[winner]["credential"]["id"] == actors[loser].credential_id
        assert results[winner]["credential"]["revoked_at"] is not None
    else:
        assert results[winner]["principal"]["id"] == actors[loser].id
        assert results[winner]["principal"]["disabled_at"] is not None
    async with sessions() as session:
        for index, actor in enumerate(actors):
            principal = await session.get(Principal, actor.id)
            credential = await session.get(ApiCredential, actor.credential_id)
            assert (principal.disabled_at is not None) == (operation == "disable" and index == loser)
            assert (credential.revoked_at is not None) == (operation == "revoke" and index == loser)
            assert (await resolve_credential(session, keys[index]) is not None) == (index != loser)
