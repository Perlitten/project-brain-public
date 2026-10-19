"""Real PostgreSQL transaction interleavings; SQLite cannot exercise row locks."""
import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

from brain.auth.principals import resolve_credential
from brain.database.models import ApiCredential, Learning, Principal
from brain.memory import learning_store, proposals
from brain.team import invitations, provisioning
from brain.team.authorization import TeamConflict, TeamDenied, human_member, live_credential
from brain.team.models import ProjectMember, TeamMember, TeamProject
from brain.team.store import create_task, request_run
from tests.test_team_foundations import caller, task_payload, run_payload, team_db as team_fixture

team_db = team_fixture

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(
    not os.getenv("TEST_TEAM_POSTGRES_URL"), reason="requires disposable PostgreSQL fixture")]


async def blocked(factory, pid):
    async with asyncio.timeout(5):
        while True:
            async with factory() as session:
                waiting = await session.scalar(text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"), {"pid": pid})
            if waiting:
                return
            await asyncio.sleep(0.01)


async def revoke(factory, principal_id=2, ready=None):
    async with factory() as session, session.begin():
        if ready is not None:
            ready.set_result(await session.scalar(text("SELECT pg_backend_pid()")))
        await provisioning.revoke_member(session, principal=await session.get(Principal, principal_id))


async def test_revoke_does_not_invert_credential_membership_locks(team_db):
    factory, _, _ = team_db
    ready = asyncio.get_running_loop().create_future()
    async with factory() as auth, auth.begin():
        await live_credential(auth, 2, 2)
        removal = asyncio.create_task(revoke(factory, ready=ready))
        try:
            await blocked(factory, await ready)
            # Old revoke owns the member and waits on our credential: this closes the deadlock cycle.
            async with asyncio.timeout(5):
                await human_member(auth, caller(2), 1)
        finally:
            await auth.rollback()
    await asyncio.wait_for(removal, 5)
    async with factory() as session:
        assert (await session.get(TeamMember, 2)).revoked_at
        assert await resolve_credential(session, "fixture-2") is None


async def test_concurrent_accepts_do_not_upgrade_project_locks(team_db, monkeypatch):
    factory, a, b = team_db
    tokens = []
    for i in range(4):
        async with factory() as session, session.begin():
            token, _ = await invitations.create_invitation(
                session, invited_by="operator", name=f"invite-{i}", projects=[str(a), str(b)][::1 if i % 2 else -1])
            tokens.append(token)
    original = invitations.fresh

    async def overlapping_read(session, model, *predicates, **kwargs):
        row = await original(session, model, *predicates, **kwargs)
        if model is TeamProject and not kwargs.get("exclusive"):
            await asyncio.sleep(0.05)  # all accepts hold SHARE before the old assignment upgrades it
        return row

    monkeypatch.setattr(invitations, "fresh", overlapping_read)

    async def accept(token):
        async with factory() as session, session.begin():
            return await invitations.accept_invitation(session, token)

    results = await asyncio.wait_for(asyncio.gather(*(accept(t) for t in tokens), return_exceptions=True), 10)
    assert all(isinstance(result, dict) for result in results), results


async def test_operator_project_resolution_does_not_upgrade_shared_lock(team_db):
    factory, a, _ = team_db
    async with factory() as one, one.begin(), factory() as two, two.begin():
        await provisioning.resolve_project(one, a)
        await provisioning.resolve_project(two, a)
        async with asyncio.timeout(2):
            await provisioning.assign_project_member(one, project_id=a, principal_id=1, role="reviewer")


async def test_concurrent_new_project_membership_assignments_lock_conflict_winner(team_db, monkeypatch):
    factory, a, _ = team_db
    original = provisioning.fresh
    initial_reads = asyncio.Barrier(3)

    async def overlapping_read(session, model, *predicates, **kwargs):
        row = await original(session, model, *predicates, **kwargs)
        if model is ProjectMember:
            if row is None:
                await initial_reads.wait()  # all three attempt the same missing membership
            elif not kwargs.get("exclusive"):
                await asyncio.sleep(0.1)  # old conflict losers both SHARE, then upgrade to UPDATE
        return row

    monkeypatch.setattr(provisioning, "fresh", overlapping_read)

    async def assign(role):
        async with factory() as session, session.begin():
            row = await provisioning.assign_project_member(session, project_id=a, principal_id=4, role=role)
            return row.role

    roles = ["reader", "contributor", "reviewer"]
    results = await asyncio.wait_for(asyncio.gather(*(assign(role) for role in roles), return_exceptions=True), 10)
    assert results == roles, results
    async with factory() as session:
        row = await session.scalar(select(ProjectMember).where(
            ProjectMember.project_id == a, ProjectMember.principal_id == 4))
        assert row.version == 2


async def test_key_resolution_does_not_queue_behind_authorization(team_db):
    factory, _, _ = team_db
    async with factory() as holder, holder.begin():
        await live_credential(holder, 2, 2)
        async with asyncio.timeout(2), factory() as resolver, resolver.begin():
            assert (await resolve_credential(resolver, "fixture-2")).id == 2
    # Usage is recorded on an unlocked key, and throttled for repeated requests.
    async with factory() as session, session.begin():
        await resolve_credential(session, "fixture-2")
    async with factory() as session:
        used = (await session.get(ApiCredential, 2)).last_used_at
        assert used is not None
    async with factory() as session, session.begin():
        await resolve_credential(session, "fixture-2")
    async with factory() as session:
        assert (await session.get(ApiCredential, 2)).last_used_at - used < timedelta(seconds=0.001)


async def test_invitation_creation_does_not_block_unrelated_team_read(team_db):
    factory, _, _ = team_db
    async with factory() as creator, creator.begin():
        await invitations.create_invitation(creator, invited_by="operator", name="independent")
        async with factory() as reader, reader.begin(), asyncio.timeout(2):
            assert await provisioning.require_team(reader)


async def test_same_name_invitations_still_serialize(team_db):
    factory, _, _ = team_db

    async def create():
        async with factory() as session, session.begin():
            result = await invitations.create_invitation(session, invited_by="operator", name="same-name")
            await asyncio.sleep(0.05)
            return result

    results = await asyncio.wait_for(asyncio.gather(create(), create(), return_exceptions=True), 5)
    assert sum(isinstance(result, tuple) for result in results) == 1, results
    assert sum(isinstance(result, TeamConflict) for result in results) == 1, results


async def test_previously_authorized_proposal_cannot_survive_revocation(team_db, monkeypatch):
    factory, _, _ = team_db
    monkeypatch.setattr(proposals, "async_session_factory", factory)
    async with factory() as session:
        await live_credential(session, 2, 2)
    await revoke(factory)
    with pytest.raises(TeamDenied):
        await proposals.create_proposal("decision", title="delayed draft", repo_path="/synthetic/A",
                                        payload={}, author_principal_id=2)


async def test_run_and_initiator_authorization_follow_revocation_lock_order(team_db):
    factory, a, _ = team_db
    async with factory() as session, session.begin():
        task = await create_task(session, caller(2), a, task_payload())
        run = await request_run(session, caller(2), a, uuid.UUID(task["id"]), run_payload())
    async with factory() as session:
        resolved = await resolve_credential(session, run["credential"])
    ready = asyncio.get_running_loop().create_future()
    async with factory() as auth, auth.begin():
        await live_credential(auth, resolved.id, resolved.credential_id)
        removal = asyncio.create_task(revoke(factory, ready=ready))
        try:
            await blocked(factory, await ready)
            async with asyncio.timeout(5):
                await live_credential(auth, 2, 2)
                await human_member(auth, caller(2), 1)
        finally:
            await auth.rollback()
    await asyncio.wait_for(removal, 5)
    async with factory() as session:
        assert await resolve_credential(session, run["credential"]) is None


async def test_direct_personal_gate_and_http_write_fence_share_lock_order(team_db, monkeypatch):
    from brain.team.write_fence import arm, request_scope

    factory, _, _ = team_db
    engine = factory.kw["bind"]
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: Learning.__table__.create(sync, checkfirst=True))
    gate_taken, continue_write = asyncio.Event(), asyncio.Event()
    original_gate = learning_store._personal_write_gate

    async def pause_after_gate(session, status, author_id):
        await original_gate(session, status, author_id)
        gate_taken.set()
        await continue_write.wait()

    async def embed(_statement):
        return None

    monkeypatch.setattr(learning_store, "_personal_write_gate", pause_after_gate)
    monkeypatch.setattr(learning_store, "async_session_factory", factory)
    monkeypatch.setattr("brain.llm.get_embedding_provider", lambda: SimpleNamespace(embed=embed, model="test"))

    async def write():
        with request_scope():
            arm(2, 2, 2, 2)
            return await learning_store.LearningStore.add_learning(
                "armed request with direct gate", status="personal", author_principal_id=2)

    writer = asyncio.create_task(write())
    removal = None
    try:
        await asyncio.wait_for(gate_taken.wait(), 3)
        ready = asyncio.get_running_loop().create_future()
        removal = asyncio.create_task(revoke(factory, ready=ready))
        await blocked(factory, await ready)
        continue_write.set()
        learning_id, _ = await asyncio.wait_for(asyncio.gather(writer, removal), 5)
        async with factory() as session:
            assert (await session.get(Learning, learning_id)).status == "personal_retired"
            assert await resolve_credential(session, "fixture-2") is None
    finally:
        continue_write.set()
        for task in (writer, removal):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (writer, removal) if task is not None), return_exceptions=True)
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync: Learning.__table__.drop(sync))


async def test_personal_insert_rechecks_membership_after_embedding(team_db, monkeypatch):
    factory, _, _ = team_db
    engine = factory.kw["bind"]
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: Learning.__table__.create(sync, checkfirst=True))
    entered, release = asyncio.Event(), asyncio.Event()

    async def embed(_statement):
        entered.set()
        await release.wait()
        return None

    monkeypatch.setattr("brain.llm.get_embedding_provider", lambda: SimpleNamespace(embed=embed, model="test"))
    monkeypatch.setattr(learning_store, "async_session_factory", factory)
    write = asyncio.create_task(learning_store.LearningStore.add_learning(
        "delayed personal note", status="personal", author_principal_id=2))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        await revoke(factory)
        release.set()
        with pytest.raises(TeamDenied):
            await asyncio.wait_for(write, 3)
        async with factory() as session:
            assert not (await session.scalars(select(Learning))).all()
    finally:
        release.set()
        await asyncio.gather(write, return_exceptions=True)
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync: Learning.__table__.drop(sync))


async def test_personal_insert_committed_first_is_retired_before_revoke_returns(team_db, monkeypatch):
    factory, _, _ = team_db
    engine = factory.kw["bind"]
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: Learning.__table__.create(sync, checkfirst=True))

    async def embed(_statement):
        return None

    monkeypatch.setattr("brain.llm.get_embedding_provider", lambda: SimpleNamespace(embed=embed, model="test"))
    # The API host can lag PostgreSQL. The revocation boundary must use the DB
    # clock after waiting for the writer, not host time or transaction-start now().
    monkeypatch.setattr(provisioning, "_now", lambda: datetime.now(timezone.utc) - timedelta(minutes=5))
    ready = asyncio.get_running_loop().create_future()
    try:
        async with factory() as writer, writer.begin():
            learning_id = await learning_store.LearningStore.add_learning(
                "committed before revoke", status="personal", author_principal_id=2, session=writer)
            removal = asyncio.create_task(revoke(factory, ready=ready))
            await blocked(factory, await ready)
        await asyncio.wait_for(removal, 5)
        async with factory() as session:
            learning = await session.get(Learning, learning_id)
            member = await session.get(TeamMember, 2)
            assert learning.status == "personal_retired"
            assert learning.created_at <= member.revoked_at
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync: Learning.__table__.drop(sync))
