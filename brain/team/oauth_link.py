"""Bind OAuth approvals to verified team members.

An OAuth access token is an ordinary scoped credential; whose credential it
becomes depends on what was asked for. Personal scopes (``core:*``, ``jobs:*``,
``harness:*``) keep the existing behaviour: one service principal per
registered client. Team scopes (``team:*``) are issued only to a verified,
active team member, on that member's own human principal, and never beyond the
permissions of the member's live project roles. The approver identity recorded
at consent is matched against ``TeamMember.verification_issuer/subject``, so
the operator decides who is a member (``brain team member add``) and OAuth
merely honours that record. The binding is evaluated on every issuance, so a
refresh after revocation fails instead of renewing the grant.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.database.models import Principal
from brain.team.authorization import ROLE_PERMISSIONS, TeamDenied
from brain.team.models import ProjectMember, TeamMember, TeamProject
from brain.team.provisioning import ADMIN_SCOPE, CREDENTIAL_ISSUER, TEAM_ID, WEB_LOGIN_ISSUER

TEAM_SCOPE_PREFIX = "team:"
# Subjects the consent page records; they describe how the approver signed in.
DEV_OPEN_SUBJECT = "dev-open"
WEB_SUBJECT_PREFIX = "web:"
# Team scopes a project role or the admin flag can grant (execute/merge/transfer are M2).
TEAM_SCOPES = sorted({f"{TEAM_SCOPE_PREFIX}{permission}" for role in ROLE_PERMISSIONS.values() for permission in role}
                     | {ADMIN_SCOPE})
MEMBER_REQUIRED = "Team scopes require approval by a verified team member"


@dataclass(frozen=True)
class TeamGrant:
    principal: Principal
    member: TeamMember
    scopes: frozenset[str]  # every team scope the member may hold right now


def team_scopes(scopes: Iterable[str]) -> list[str]:
    return [scope for scope in scopes if scope.startswith(TEAM_SCOPE_PREFIX)]


def approver_identity(subject: str | None) -> tuple[str, str] | None:
    """Map the subject recorded at consent to the ``(issuer, subject)`` pair a member is verified under."""
    if not subject or subject == DEV_OPEN_SUBJECT:
        return None
    if subject.startswith(WEB_SUBJECT_PREFIX):
        user = subject[len(WEB_SUBJECT_PREFIX):]
        return (WEB_LOGIN_ISSUER, user) if user else None
    return CREDENTIAL_ISSUER, subject


async def member_grant(session: AsyncSession, issuer: str, subject: str, team_id: int = TEAM_ID) -> TeamGrant | None:
    """The live member verified under ``issuer``/``subject`` and the team scopes their roles allow."""
    member = (await session.execute(
        select(TeamMember).where(TeamMember.team_id == team_id, TeamMember.verification_issuer == issuer,
                                 TeamMember.verification_subject == subject))).scalar_one_or_none()
    if member is None or not member.verified_at or member.revoked_at:
        return None
    principal = await session.get(Principal, member.principal_id)
    if principal is None or principal.kind != "human" or principal.disabled_at:
        return None
    roles = (await session.execute(
        select(ProjectMember.role).join(TeamProject, TeamProject.id == ProjectMember.project_id)
        .where(ProjectMember.principal_id == principal.id, ProjectMember.revoked_at.is_(None),
               TeamProject.disabled_at.is_(None)))).scalars().all()
    scopes = {f"{TEAM_SCOPE_PREFIX}{permission}" for role in roles for permission in ROLE_PERMISSIONS.get(role, ())}
    if member.is_admin:
        scopes.add(ADMIN_SCOPE)
    return TeamGrant(principal=principal, member=member, scopes=frozenset(scopes))


async def bind_team_scopes(session: AsyncSession, subject: str | None, scopes: Iterable[str]) -> TeamGrant | None:
    """Return the member grant backing the ``team:*`` scopes in ``scopes``.

    ``None`` means no team scope was requested and the personal flow applies
    unchanged. ``TeamDenied`` means the approver is not a verified member or
    asked for a team scope outside the member's roles."""
    requested = team_scopes(scopes)
    if not requested:
        return None
    identity = approver_identity(subject)
    grant = await member_grant(session, *identity) if identity is not None else None
    if grant is None:
        raise TeamDenied(MEMBER_REQUIRED)
    outside = sorted(set(requested) - grant.scopes)
    if outside:
        raise TeamDenied(f"Team scopes outside the member's roles: {', '.join(outside)}")
    return grant
