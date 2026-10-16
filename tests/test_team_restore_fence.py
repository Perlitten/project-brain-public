"""Restore fence: a restored team snapshot honours nothing until it is reconciled.

The real PostgreSQL restore that writes the fence is ``tests/test_team_restore.py``;
these cases pin the fence and reconciliation semantics on the shared team fixture.
"""
import pytest
from sqlalchemy import select, update

from brain.auth.principals import resolve_credential
from brain.config.settings import settings
from brain.database.models import ApiCredential
from brain.team import invitations, provisioning, restore_fence
from brain.team.authorization import Permission, TeamConflict, TeamDenied, authorize, team_state
from brain.team.models import TeamAccessFence, TeamMember
from tests.test_team_foundations import NOW, caller
from tests.test_team_foundations import team_db as foundation_db

team_db = foundation_db


async def fence(factory):
    async with factory() as session, session.begin():
        session.add(TeamAccessFence(reason="restored from a synthetic snapshot"))


@pytest.mark.asyncio
async def test_fence_refuses_every_credential_and_keeps_team_state(team_db, monkeypatch):
    factory, a, _ = team_db
    monkeypatch.setattr(settings, "BRAIN_TEAM_MODE", False)
    await fence(factory)
    async with factory() as session, session.begin():
        # Even with no live member left, a fenced Brain never falls back to solo (shared key, dev-open).
        await session.execute(update(TeamMember).values(revoked_at=NOW))
        assert await restore_fence.access_fenced(session)
        assert await team_state(session) == "team"
        assert await resolve_credential(session, "fixture-2") is None
        with pytest.raises(TeamDenied, match="fenced"):
            await authorize(session, caller(4), a, Permission.READ)
    monkeypatch.setattr(settings, "BRAIN_STORAGE_MODE", "lite")
    async with factory() as session:
        assert not await restore_fence.access_fenced(session)


@pytest.mark.asyncio
async def test_reconcile_replays_revocations_and_rotates_credentials(team_db):
    factory, a, _ = team_db
    await fence(factory)
    async with factory() as session, session.begin():
        reviewed = await restore_fence.review(session)
        assert reviewed.summary()["team_access"] == "pending_reconciliation"
        assert reviewed.summary()["to_revoke_unless_kept"]["credentials"] == 4
        with pytest.raises(TeamConflict, match="attested digest"):
            await restore_fence.reconcile(session, attest="0" * 64)
    async with factory() as session, session.begin():
        # Member 1 was revoked after the snapshot: the operator replays it, which stales the old digest.
        await provisioning.revoke_member(session, principal=await provisioning.resolve_principal(session, 1))
        with pytest.raises(TeamConflict, match="attested digest"):
            await restore_fence.reconcile(session, attest=reviewed.digest)
    async with factory() as session, session.begin():
        assert await restore_fence.access_fenced(session)
        current = await restore_fence.review(session)
        result = await restore_fence.reconcile(session, attest=current.digest.upper())
        assert result["credentials"] == "rotated" and result["revoked"]["credentials"] == 4
    async with factory() as session, session.begin():
        assert not await restore_fence.access_fenced(session)
        assert (await session.execute(select(ApiCredential).where(ApiCredential.revoked_at.is_(None)))).first() is None
        for raw in ("fixture-1", "fixture-2", "fixture-4"):
            assert await resolve_credential(session, raw) is None
        raw, _ = await provisioning.create_human_principal(session, "human-2")
        member = await resolve_credential(session, raw)
        await authorize(session, member, a, Permission.READ)
        with pytest.raises(TeamDenied):
            await authorize(session, caller(1), a, Permission.READ)
        with pytest.raises(TeamConflict, match="No restore fence"):
            await restore_fence.reconcile(session, attest=current.digest)


@pytest.mark.asyncio
async def test_reconcile_can_keep_credentials_of_a_current_snapshot(team_db):
    factory, a, _ = team_db
    await fence(factory)
    async with factory() as session, session.begin():
        digest = (await restore_fence.review(session)).digest
        result = await restore_fence.reconcile(session, attest=digest, keep_credentials=True)
        assert result["credentials"] == "kept" and result["revoked"]["credentials"] == 0
    async with factory() as session, session.begin():
        assert (await resolve_credential(session, "fixture-2")).id == 2
        await authorize(session, caller(2), a, Permission.READ)
        cleared = (await session.execute(select(TeamAccessFence))).scalar_one()
        assert cleared.cleared_at is not None and digest in cleared.cleared_by


@pytest.mark.asyncio
async def test_pending_invitations_are_fenced_and_rotated(team_db):
    factory, _, _ = team_db
    async with factory() as session, session.begin():
        # Revoked after the snapshot, so live again in the restored rows.
        token, _ = await invitations.create_invitation(session, invited_by="operator:cli", name="invitee")
    await fence(factory)
    async with factory() as session, session.begin():
        with pytest.raises(TeamConflict, match="fenced"):
            await invitations.accept_invitation(session, token)
        reviewed = await restore_fence.review(session)
        assert reviewed.summary()["to_revoke_unless_kept"]["pending_invitations"] == 1
        result = await restore_fence.reconcile(session, attest=reviewed.digest)
        assert result["revoked"]["pending_invitations"] == 1
    async with factory() as session, session.begin():
        with pytest.raises(invitations.InvitationRejected):
            await invitations.accept_invitation(session, token)
