"""Single-use team invitations: invited -> verified -> active -> revoked.

A team administrator (over the API) or a trusted operator (``brain team
invite`` against the database) issues an invitation for an operator-chosen identity:
a principal name, an optional email, target projects, a project role and
whether the invitee administers the team. The plaintext token is returned
once; only its SHA-256 is stored.

"Verified" means exactly this: the invitee presented the unexpired,
unrevoked, single-use token an administrator issued for that identity. The
server then creates the human principal, records team membership under the
issuer ``brain-invitation`` with subject ``invitation:<id>``, grants the
project roles and returns a named member credential once. It is not OIDC and
not proof that the invitee controls the email address; whoever holds the
token becomes the member, so deliver it over a channel you trust.

The state is derived, never stored: ``revoked`` (invitation revoked, or the
issued membership, principal or credential no longer live), ``active`` (the
issued credential has been used), ``verified`` (accepted, credential not used
yet), ``expired`` (never accepted before ``expires_at``) and ``invited``.

Like ``provisioning``, these functions perform no caller authorization; the
CLI runs them as a trusted operator and the HTTP router authorizes first.
"""
from __future__ import annotations

import hmac
import re
import secrets
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from brain.auth.principals import hash_credential, mint_credential
from brain.database.models import ApiCredential, Principal
from brain.team.authorization import ROLE_PERMISSIONS, TeamConflict, TeamDenied, fresh
from brain.team.models import ProjectMember, TeamInvitation, TeamMember, TeamProject
from brain.team.provisioning import (
    TEAM_ID,
    TeamNotFound,
    _iso,
    _now,
    add_member,
    assign_project_member,
    member_dict,
    member_scopes,
    project_member_dict,
    require_team,
    resolve_project,
    revoke_member,
)

INVITATION_ISSUER = "brain-invitation"
TOKEN_PREFIX = "pbi_"
MAX_TOKEN_LENGTH = 128
DEFAULT_TTL_HOURS = 72
MAX_TTL_HOURS = 720
MAX_PROJECTS = 16
NAME_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9._@+-]{0,63}")
EMAIL_PATTERN = re.compile(r"[^@\s]{1,64}@[^@\s]{1,189}")
REJECTED = "Invitation token is invalid, expired or revoked"


class InvitationRejected(TeamDenied):
    """The presented token is unknown, expired, revoked or its membership was revoked (one uniform answer)."""


def generate_token() -> str:
    return f"{TOKEN_PREFIX}{secrets.token_urlsafe(32)}"


def _subject(invitation: TeamInvitation) -> str:
    return f"invitation:{invitation.id}"


async def _live_member_exists(session: AsyncSession, team_id: int) -> bool:
    found = (await session.execute(select(TeamMember.principal_id).where(
        TeamMember.team_id == team_id, TeamMember.revoked_at.is_(None), TeamMember.verified_at.is_not(None))
        .limit(1))).scalar_one_or_none()
    return found is not None


async def _issued(session: AsyncSession, invitation: TeamInvitation):
    if invitation.accepted_principal_id is None:
        return None, None, None
    principal = await session.get(Principal, invitation.accepted_principal_id, populate_existing=True)
    member = await session.get(TeamMember, invitation.accepted_principal_id, populate_existing=True)
    credential = None
    if invitation.accepted_credential_id is not None:
        credential = await session.get(ApiCredential, invitation.accepted_credential_id, populate_existing=True)
    return principal, member, credential


def derive_state(invitation: TeamInvitation, principal: Principal | None, member: TeamMember | None,
                 credential: ApiCredential | None, now: datetime | None = None) -> str:
    now = now or _now()
    if invitation.revoked_at is not None:
        return "revoked"
    if invitation.accepted_at is not None:
        if (principal is None or principal.disabled_at is not None or member is None or member.revoked_at is not None
                or credential is None or credential.revoked_at is not None):
            return "revoked"
        return "active" if credential.last_used_at is not None else "verified"
    return "expired" if invitation.expires_at <= now else "invited"


def invitation_dict(invitation: TeamInvitation, state: str) -> dict:
    """Public view: never includes the token or its hash."""
    return {"id": str(invitation.id), "team_id": invitation.team_id, "state": state,
            "principal_name": invitation.principal_name, "email": invitation.email,
            "project_ids": list(invitation.project_ids or []), "role": invitation.role,
            "is_admin": bool(invitation.is_admin), "created_at": _iso(invitation.created_at),
            "expires_at": _iso(invitation.expires_at), "invited_by": invitation.invited_by,
            "invited_by_principal_id": invitation.invited_by_principal_id,
            "accepted_at": _iso(invitation.accepted_at), "accepted_principal_id": invitation.accepted_principal_id,
            "revoked_at": _iso(invitation.revoked_at), "revoked_by": invitation.revoked_by}


async def describe(session: AsyncSession, invitation: TeamInvitation) -> dict:
    return invitation_dict(invitation, derive_state(invitation, *await _issued(session, invitation)))


async def create_invitation(session: AsyncSession, *, invited_by: str, invited_by_principal_id: int | None = None,
                            projects: list[str] | None = None, role: str = "contributor", is_admin: bool = False,
                            email: str | None = None, name: str | None = None,
                            expires_hours: int = DEFAULT_TTL_HOURS, team_id: int = TEAM_ID) -> tuple[str, dict]:
    """Issue an invitation; returns the plaintext token (shown once) and the public view."""
    await require_team(session, team_id)
    if role not in ROLE_PERMISSIONS:
        raise TeamConflict("Unknown project role")
    if not 1 <= expires_hours <= MAX_TTL_HOURS:
        raise TeamConflict(f"Expiry must be between 1 and {MAX_TTL_HOURS} hours")
    targets = list(dict.fromkeys(item.strip() for item in (projects or []) if item.strip()))
    if len(targets) > MAX_PROJECTS:
        raise TeamConflict(f"An invitation can target at most {MAX_PROJECTS} projects")
    project_ids = []
    for target in targets:
        project = await resolve_project(session, target, team_id)
        if project.team_id != team_id or project.disabled_at is not None:
            raise TeamConflict(f"Project {target!r} is disabled or belongs to another team")
        project_ids.append(str(project.id))
    if email is not None:
        email = email.strip() or None
        if email is not None and not EMAIL_PATTERN.fullmatch(email):
            raise TeamConflict("Email address is malformed")
    name = (name or "").strip() or f"invitee-{secrets.token_hex(6)}"
    if not NAME_PATTERN.fullmatch(name):
        raise TeamConflict("Principal name must start with a letter and use only letters, digits and ._@+-")
    if await fresh(session, Principal, Principal.name == name) is not None:
        raise TeamConflict(f"Principal name {name!r} is already taken")
    now = _now()
    pending = (await session.execute(select(TeamInvitation.id).where(
        TeamInvitation.team_id == team_id, TeamInvitation.principal_name == name,
        TeamInvitation.accepted_at.is_(None), TeamInvitation.revoked_at.is_(None),
        TeamInvitation.expires_at > now).limit(1))).scalar_one_or_none()
    if pending is not None:
        raise TeamConflict(f"A pending invitation for {name!r} already exists; revoke it first")
    if not is_admin and not await _live_member_exists(session, team_id):
        # The first member flips a team of one into a team and retires the shared
        # key, so it must be able to administer the team or nobody could.
        raise TeamConflict("The first invitation must grant team administration so the team keeps an administrator")
    raw = generate_token()
    invitation = TeamInvitation(team_id=team_id, token_hash=hash_credential(raw), principal_name=name, email=email,
                                project_ids=project_ids, role=role, is_admin=is_admin,
                                expires_at=now + timedelta(hours=expires_hours), invited_by=invited_by,
                                invited_by_principal_id=invited_by_principal_id)
    session.add(invitation)
    await session.flush()
    await session.refresh(invitation)
    return raw, invitation_dict(invitation, "invited")


async def list_invitations(session: AsyncSession, team_id: int = TEAM_ID) -> list[dict]:
    rows = (await session.execute(select(TeamInvitation).where(TeamInvitation.team_id == team_id)
                                  .order_by(TeamInvitation.created_at, TeamInvitation.id))).scalars().all()
    return [await describe(session, invitation) for invitation in rows]


async def _accepted_result(session: AsyncSession, invitation: TeamInvitation, credential: str | None) -> dict:
    principal, member, _ = await _issued(session, invitation)
    memberships = []
    for project_id in invitation.project_ids or []:
        row = await fresh(session, ProjectMember, ProjectMember.project_id == uuid.UUID(project_id),
                          ProjectMember.principal_id == invitation.accepted_principal_id)
        if row is not None:
            memberships.append(project_member_dict(row))
    return {"invitation": await describe(session, invitation), "member": member_dict(member, principal),
            "project_memberships": memberships, "credential": credential}


async def accept_invitation(session: AsyncSession, raw_token: str) -> dict:
    """Redeem a token: create the verified member and return its credential once.

    Retrying with the same token after success returns the same member with
    ``credential: None``. The invitation row is locked, so concurrent
    redemptions serialize and issue exactly one credential."""
    raw_token = (raw_token or "").strip()
    if not raw_token.startswith(TOKEN_PREFIX) or len(raw_token) > MAX_TOKEN_LENGTH:
        raise InvitationRejected(REJECTED)
    digest = hash_credential(raw_token)
    invitation = await fresh(session, TeamInvitation, TeamInvitation.token_hash == digest, exclusive=True)
    if invitation is None or not hmac.compare_digest(invitation.token_hash, digest) or invitation.revoked_at:
        raise InvitationRejected(REJECTED)
    if invitation.accepted_at is not None:
        if derive_state(invitation, *await _issued(session, invitation)) == "revoked":
            raise InvitationRejected(REJECTED)
        return await _accepted_result(session, invitation, None)
    if invitation.expires_at <= _now():
        raise InvitationRejected(REJECTED)
    await require_team(session, invitation.team_id)
    if invitation.invited_by_principal_id is not None:
        # An invitation carries its issuer's authority: once that administrator
        # is revoked or demoted, their unredeemed invitations stop working.
        inviter = await fresh(session, TeamMember, TeamMember.principal_id == invitation.invited_by_principal_id,
                              TeamMember.team_id == invitation.team_id)
        if inviter is None or inviter.revoked_at is not None or not inviter.verified_at or not inviter.is_admin:
            raise TeamConflict("The inviting administrator no longer administers this team; ask for a new invitation")
    if not invitation.is_admin and not await _live_member_exists(session, invitation.team_id):
        raise TeamConflict("This Brain has no team members yet; the first accepted invitation must grant administration")
    if await fresh(session, Principal, Principal.name == invitation.principal_name) is not None:
        raise TeamConflict(f"Principal name {invitation.principal_name!r} is already taken; ask for a new invitation")
    projects = []
    for project_id in invitation.project_ids or []:
        project = await fresh(session, TeamProject, TeamProject.id == uuid.UUID(project_id))
        if project is None or project.disabled_at is not None or project.team_id != invitation.team_id:
            raise TeamConflict("An invited project no longer exists or is disabled; ask for a new invitation")
        projects.append(project)
    raw_key, credential = await mint_credential(session, name=invitation.principal_name,
                                                scopes=member_scopes(invitation.is_admin), kind="human")
    await session.flush()
    principal = await fresh(session, Principal, Principal.id == credential.principal_id)
    await add_member(session, principal=principal, issuer=INVITATION_ISSUER, subject=_subject(invitation),
                     is_admin=bool(invitation.is_admin), team_id=invitation.team_id)
    for project in projects:
        await assign_project_member(session, project_id=project.id, principal_id=principal.id, role=invitation.role)
    invitation.accepted_at = _now()
    invitation.accepted_principal_id = principal.id
    invitation.accepted_credential_id = credential.id
    await session.flush()
    return await _accepted_result(session, invitation, raw_key)


async def revoke_invitation(session: AsyncSession, invitation_id: uuid.UUID, *, revoked_by: str,
                            revoke_issued_member: bool = False, protected_principal_id: int | None = None) -> dict:
    """Revoke an invitation; an accepted one is revoked only together with the access it granted.

    An accepted invitation always needs ``revoke_issued_member``: revoking it
    revokes the membership, every project role and every credential of the
    principal it created (a revoked credential must not regain implicit
    administration if the Brain falls back to a team of one). Without the flag
    the call fails instead of silently revoking only the token. Repeating a
    revocation is a no-op that returns the recorded state."""
    invitation = await fresh(session, TeamInvitation, TeamInvitation.id == invitation_id, exclusive=True)
    if invitation is None:
        raise TeamNotFound("Invitation not found")
    if invitation.revoked_at is not None:
        return await describe(session, invitation)
    now = _now()
    if invitation.accepted_principal_id is not None:
        if not revoke_issued_member:
            raise TeamConflict(f"Invitation was already accepted by principal {invitation.accepted_principal_id}; "
                               "pass revoke_member to also revoke that member and its credentials")
        if protected_principal_id == invitation.accepted_principal_id:
            raise TeamConflict("Administrators cannot revoke their own membership")
        principal, member, _ = await _issued(session, invitation)
        if member is not None and principal is not None:
            await revoke_member(session, principal=principal)
        await session.execute(update(ApiCredential).where(
            ApiCredential.principal_id == invitation.accepted_principal_id, ApiCredential.revoked_at.is_(None))
            .values(revoked_at=now))
    invitation.revoked_at = now
    invitation.revoked_by = revoked_by
    await session.flush()
    return await describe(session, invitation)
