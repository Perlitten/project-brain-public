"""PostgreSQL row-lock order of team revocation, onboarding and authentication.

Each test reproduces one interleaving the multi-user load harness hit
(docs/reports/team-memory-load-e2e-2026-10-18.md): sessions run on separate
connections and are stepped with asyncio events, so the lock order, not
timing luck, decides the outcome. A deadlock surfaces as SQLSTATE 40P01; a
stall surfaces as ``asyncio.TimeoutError``.
"""
import asyncio
import os
import uuid

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError, OperationalError

from apps.api import team_access
from brain.auth.principals import resolve_credential
from brain.database.models import ApiCredential, Decision, MemoryProposal, Principal, Rule
from brain.team import invitations, provisioning
from brain.team.authorization import Permission, TeamDenied, human_member, live_credential, team_administrator
from brain.team.models import AgentRun, ProjectMember, TeamActivityEvent, TeamMember, TeamProject
from brain.team.store import create_task, request_run
from tests.test_team_foundations import caller, run_payload, task_payload, team_db as team_fixture
from tests.test_team_transport import client_for, personal_app

team_db = team_fixture
pytestmark = [pytest.mark.team_state,
              pytest.mark.skipif(not os.getenv("TEST_TEAM_POSTGRES_URL"), reason="row-lock order needs PostgreSQL")]
STALL = 3.0  # seconds; a convoy or a missed deadlock shows up as a timeout well above normal latency


def sqlstate(exc: BaseException) -> str | None:
    orig = getattr(exc, "orig", None)
    return getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)


async def revoke(factory, principal_id: int):
    async with factory() as session, session.begin():
        principal = await session.get(Principal, principal_id)
        return await provisioning.revoke_member(session, principal=principal, actor_id=4)


async def credential_revoked(factory, credential_id: int) -> bool:
    async with factory() as session:
        return (await session.get(ApiCredential, credential_id)).revoked_at is not None


# 1. Revocation against a member's in-flight authorization.

@pytest.mark.asyncio
async def test_revoke_during_member_authorization_neither_deadlocks_nor_leaves_the_key_live(team_db):
    factory, _, _ = team_db
    authorized = asyncio.Event()

    async def member_request():
        async with factory() as session, session.begin():
            live = await live_credential(session, 2, 2)  # principals S, api_credentials S
            authorized.set()
            await asyncio.sleep(0.5)  # the revoke reaches its first lock meanwhile
            await human_member(session, live, 1)  # team_members S

    async def admin_revoke():
        await authorized.wait()
        return await revoke(factory, 2)

    member, revoked = await asyncio.wait_for(asyncio.gather(member_request(), admin_revoke()), STALL + 2)
    assert member is None and revoked.revoked_at is not None
    assert await credential_revoked(factory, 2)


@pytest.mark.asyncio
async def test_revoke_during_run_authorization_neither_deadlocks_nor_leaves_run_keys_live(team_db):
    """A run request locks its own principal, then its initiator's; revocation locks the runs first."""
    factory, a, _ = team_db
    async with factory() as session, session.begin():
        task = await create_task(session, caller(2), a, task_payload())
        run = await request_run(session, caller(2), a, uuid.UUID(task["id"]), run_payload())
        grant = await session.get(AgentRun, uuid.UUID(run["id"]))
        run_principal, run_credential = grant.principal_id, grant.credential_id
    authorized = asyncio.Event()

    async def run_request():
        async with factory() as session, session.begin():
            await live_credential(session, run_principal, run_credential)  # authorize(): the run first
            await session.execute(select(AgentRun).where(AgentRun.principal_id == run_principal)
                                  .with_for_update(read=True))
            authorized.set()
            await asyncio.sleep(0.5)
            initiator = await live_credential(session, 2, 2)
            await human_member(session, initiator, 1)

    async def admin_revoke():
        await authorized.wait()
        return await revoke(factory, 2)

    await asyncio.wait_for(asyncio.gather(run_request(), admin_revoke()), STALL + 2)
    assert await credential_revoked(factory, 2) and await credential_revoked(factory, run_credential)


@pytest.mark.asyncio
async def test_revoke_retries_a_deadlock_victim_and_commits_once(team_db, monkeypatch):
    factory, _, _ = team_db
    real = provisioning.withdraw_author_proposals
    calls = 0

    class Deadlock(Exception):
        sqlstate = "40P01"

    async def victim_once(session, principal_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OperationalError("UPDATE memory_proposals", {}, Deadlock("deadlock detected"))
        return await real(session, principal_id)

    monkeypatch.setattr(provisioning, "withdraw_author_proposals", victim_once)
    member = await revoke(factory, 2)
    assert calls == 2 and member.revoked_at is not None
    assert await credential_revoked(factory, 2)
    async with factory() as session:
        events = (await session.execute(select(func.count()).select_from(TeamActivityEvent).where(
            TeamActivityEvent.kind == "membership.revoked", TeamActivityEvent.subject_principal_id == 2))).scalar_one()
        roles = (await session.execute(select(ProjectMember.revoked_at).where(ProjectMember.principal_id == 2)))
        assert events == 1 and all(revoked_at is not None for revoked_at, in roles)


@pytest.mark.asyncio
async def test_revoke_gives_up_after_bounded_retries_without_committing(team_db, monkeypatch):
    factory, _, _ = team_db

    class Deadlock(Exception):
        sqlstate = "40P01"

    async def always_victim(session, principal_id):
        raise OperationalError("UPDATE memory_proposals", {}, Deadlock("deadlock detected"))

    monkeypatch.setattr(provisioning, "withdraw_author_proposals", always_victim)
    with pytest.raises(DBAPIError) as raised:
        await revoke(factory, 2)
    assert sqlstate(raised.value) == "40P01"
    assert not await credential_revoked(factory, 2)
    async with factory() as session:
        assert (await session.get(TeamMember, 2)).revoked_at is None


# 2. Writes already authorized when the revocation commits.

def _decision(**changes):
    values = dict(title="synthetic", description="no provider calls", repo_path="/synthetic/A")
    values.update(changes)
    return Decision(**values)


@pytest.mark.asyncio
async def test_write_authorized_before_revocation_is_refused_after_it(team_db, monkeypatch):
    factory, _, _ = team_db
    from brain.team import write_fence

    monkeypatch.setattr(team_access, "async_session_factory", factory)
    with write_fence.request_scope():  # one HTTP request of member 2
        await team_access.repository_authority(caller(2), Permission.PROPOSE, repository_id=1)
        with write_fence.request_scope():  # the administrator's request, meanwhile
            await revoke(factory, 2)
        with pytest.raises(TeamDenied):
            async with factory() as session, session.begin():
                session.add(_decision())
        with pytest.raises(TeamDenied):
            async with factory() as session, session.begin():
                await session.execute(Rule.__table__.insert().values(id="rule-after-revoke", name="t"))
        with pytest.raises(TeamDenied):
            async with factory() as session, session.begin():
                await session.execute(update(MemoryProposal).values(title="t"))
    async with factory() as session:
        assert (await session.execute(select(func.count()).select_from(Decision))).scalar_one() == 0
        assert (await session.execute(select(func.count()).select_from(Rule))).scalar_one() == 0
    async with factory() as session, session.begin():  # outside a member request nothing is fenced
        session.add(_decision())


@pytest.mark.asyncio
async def test_unarmed_request_and_live_member_writes_pass(team_db, monkeypatch):
    factory, _, _ = team_db
    from brain.team import write_fence

    monkeypatch.setattr(team_access, "async_session_factory", factory)
    with write_fence.request_scope():
        async with factory() as session, session.begin():  # authenticated, not yet authorized: no fence
            session.add(_decision(title="before"))
        await team_access.repository_authority(caller(2), Permission.PROPOSE, repository_id=1)
        async with factory() as session, session.begin():
            session.add(_decision(title="armed"))
            await session.flush()
            session.add(_decision(title="second flush, same transaction"))
    async with factory() as session:
        assert (await session.execute(select(func.count()).select_from(Decision))).scalar_one() == 3


@pytest.mark.asyncio
async def test_write_in_flight_lands_before_revocation_without_deadlock(team_db, monkeypatch):
    factory, _, _ = team_db
    from brain.team import write_fence

    monkeypatch.setattr(team_access, "async_session_factory", factory)
    wrote = asyncio.Event()

    async def member_write():
        with write_fence.request_scope():
            await team_access.repository_authority(caller(2), Permission.PROPOSE, repository_id=1)
            async with factory() as session, session.begin():
                session.add(_decision())
                await session.flush()  # the fence locks the member's principal for this transaction
                wrote.set()
                await asyncio.sleep(0.5)
                session.add(MemoryProposal(kind="decision", author_principal_id=2, payload={}, title="t",
                                           state="pending"))

    async def admin_revoke():
        await wrote.wait()
        with write_fence.request_scope():
            return await revoke(factory, 2)

    task = asyncio.create_task(member_write())
    revoked = await asyncio.wait_for(admin_revoke(), STALL + 2)
    await task
    async with factory() as session:
        decision = (await session.execute(select(Decision))).scalar_one()
        proposal = (await session.execute(select(MemoryProposal))).scalar_one()
    assert decision.created_at <= revoked.revoked_at
    assert proposal.state == "withdrawn"  # the revocation waited for the write and saw its proposal


@pytest.mark.asyncio
async def test_member_revoked_mid_request_gets_403_and_writes_nothing(team_db, monkeypatch):
    factory, _, _ = team_db
    import contextvars

    from apps.api.routers import memory_review
    from brain.team.write_fence import WriteFenceMiddleware

    app = personal_app(factory, monkeypatch)
    app.add_middleware(WriteFenceMiddleware)
    app.add_exception_handler(TeamDenied, team_access.team_denied_response)
    authorized = memory_review.proposal_authority

    async def authorize_then_lose_membership(caller_, repo_path):
        authority = await authorized(caller_, repo_path)
        # The administrator's request runs in its own context, as a separate HTTP request would.
        await asyncio.create_task(revoke(factory, 2), context=contextvars.Context())
        return authority

    monkeypatch.setattr(memory_review, "proposal_authority", authorize_then_lose_membership)
    async with client_for(app) as client:
        response = await client.post("/decisions", headers={"X-API-Key": "fixture-2"},
                                     json={"title": "late", "description": "d", "repo_path": "/synthetic/A"})
    assert response.status_code == 403, response.text
    async with factory() as session:
        assert (await session.execute(select(func.count()).select_from(MemoryProposal))).scalar_one() == 0


@pytest.mark.asyncio
async def test_api_app_installs_the_write_fence():
    from apps.api.main import app
    from brain.team.write_fence import WriteFenceMiddleware

    assert any(middleware.cls is WriteFenceMiddleware for middleware in app.user_middleware)
    assert app.exception_handlers.get(TeamDenied) is team_access.team_denied_response


# 3. Concurrent onboarding.

@pytest.mark.asyncio
async def test_concurrent_accepts_on_one_project_do_not_deadlock(team_db, monkeypatch):
    factory, a, _ = team_db
    tokens = []
    for name in ("onboard-1", "onboard-2"):
        async with factory() as session, session.begin():
            token, _ = await invitations.create_invitation(session, invited_by="principal:4", projects=[str(a)],
                                                           name=name)
            tokens.append(token)
    real = invitations.assign_project_member
    both_read_projects = asyncio.Barrier(2)

    async def after_both_read(session, **kwargs):
        try:  # let the second accept catch up so both hold their project locks; never wait forever
            await asyncio.wait_for(both_read_projects.wait(), 1.0)
        except (asyncio.TimeoutError, asyncio.BrokenBarrierError):
            pass
        return await real(session, **kwargs)

    monkeypatch.setattr(invitations, "assign_project_member", after_both_read)

    async def accept(token):
        async with factory() as session, session.begin():
            return await invitations.accept_invitation(session, token)

    results = await asyncio.wait_for(asyncio.gather(*(accept(token) for token in tokens)), STALL + 4)
    assert all(result["credential"] for result in results)
    async with factory() as session:
        roles = (await session.execute(select(func.count()).select_from(ProjectMember).where(
            ProjectMember.project_id == a, ProjectMember.revoked_at.is_(None)))).scalar_one()
    assert roles == 3 + 2


# 4. The invitation critical section and the credential write on every request.

async def _hold_open_invitation(factory, name, opened: asyncio.Event, release: asyncio.Event):
    async with factory() as session, session.begin():
        await team_administrator(session, caller(4))
        await invitations.create_invitation(session, invited_by="principal:4", name=name)
        opened.set()
        await release.wait()


@pytest.mark.asyncio
async def test_open_invitation_does_not_stall_onboarding_or_other_invitations(team_db):
    factory, a, _ = team_db
    async with factory() as session, session.begin():
        token, _ = await invitations.create_invitation(session, invited_by="principal:4", projects=[str(a)],
                                                       name="already-invited")
    opened, release = asyncio.Event(), asyncio.Event()
    holder = asyncio.create_task(_hold_open_invitation(factory, "slow-admin", opened, release))
    await opened.wait()
    try:
        async def accept():
            async with factory() as session, session.begin():
                return await invitations.accept_invitation(session, token)

        async def other_invitation():
            async with factory() as session, session.begin():
                return await invitations.create_invitation(session, invited_by="principal:4", name="other-name")

        accepted, created = await asyncio.wait_for(asyncio.gather(accept(), other_invitation()), STALL)
        assert accepted["credential"] and created[1]["state"] == "invited"
    finally:
        release.set()
        await holder


@pytest.mark.asyncio
async def test_open_admin_transaction_does_not_stall_the_admin_key_elsewhere(team_db):
    factory, _, _ = team_db
    opened, release = asyncio.Event(), asyncio.Event()
    holder = asyncio.create_task(_hold_open_invitation(factory, "slow-admin", opened, release))
    await opened.wait()
    try:
        async def authenticate():
            async with factory() as session, session.begin():
                return await resolve_credential(session, "fixture-4")

        resolved = await asyncio.wait_for(authenticate(), STALL)
        assert resolved is not None and resolved.id == 4
    finally:
        release.set()
        await holder
    async with factory() as session, session.begin():
        await resolve_credential(session, "fixture-4")
        used = (await session.get(ApiCredential, 4)).last_used_at
    assert used is not None


@pytest.mark.asyncio
async def test_team_route_blocked_on_a_lock_fails_fast_with_503(team_db, monkeypatch):
    factory, a, _ = team_db
    from brain.config.settings import settings

    monkeypatch.setattr(settings, "BRAIN_TEAM_LOCK_TIMEOUT_MS", 300)
    app = personal_app(factory, monkeypatch)
    locked, release = asyncio.Event(), asyncio.Event()

    async def hold_project():
        async with factory() as session, session.begin():
            await session.execute(select(TeamProject).where(TeamProject.id == a).with_for_update())
            locked.set()
            await release.wait()

    holder = asyncio.create_task(hold_project())
    await locked.wait()
    try:
        async with client_for(app) as client:
            started = asyncio.get_running_loop().time()
            response = await asyncio.wait_for(client.put(f"/team/projects/{a}/members/1", json={"role": "reviewer"},
                                                         headers={"X-API-Key": "fixture-4"}), STALL)
            elapsed = asyncio.get_running_loop().time() - started
        assert response.status_code == 503 and response.headers.get("retry-after")
        assert elapsed < 2.0
    finally:
        release.set()
        await holder
    async with factory() as session:
        role = (await session.execute(select(ProjectMember.role).where(
            ProjectMember.project_id == a, ProjectMember.principal_id == 1))).scalar_one()
    assert role == "reader"


@pytest.mark.asyncio
async def test_team_transactions_bound_lock_waits_and_idle_time(team_db):
    factory, _, _ = team_db
    from brain.team.locking import bound_team_transaction

    async with factory() as session, session.begin():
        await bound_team_transaction(session)
        settings_now = dict((await session.execute(text(
            "SELECT name, setting FROM pg_settings WHERE name IN "
            "('lock_timeout', 'idle_in_transaction_session_timeout')"))).all())
    assert int(settings_now["lock_timeout"]) > 0
    assert int(settings_now["idle_in_transaction_session_timeout"]) > 0
    async with factory() as session:  # SET LOCAL: the next transaction on the connection is unbounded again
        assert (await session.execute(text("SHOW lock_timeout"))).scalar_one() == "0"
