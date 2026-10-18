"""Idempotent team provisioning primitives.

These functions perform no caller authorization: the CLI runs them as a
trusted operator action against the database (the offline provisioning path
of the team collaboration ADR), and the HTTP router calls them only after the
``team_administrator`` / ``administrator`` checks. Every operation is safe to
repeat: re-running it with the same arguments returns the existing row, while
contradictory arguments raise ``TeamConflict`` instead of rewriting state.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from brain.auth.principals import mint_credential
from brain.team.activity import record_event
from brain.database.models import ApiCredential, OAuthRefreshToken, Principal, Repository
from brain.memory.proposals import withdraw_author_proposals
from brain.team.authorization import ROLE_PERMISSIONS, Permission, TeamConflict, TeamDenied, fresh, permissions
from brain.team.locking import retry_transient
from brain.team.models import (AgentRun, ProjectMember, ProjectRepository, Team, TeamExecutionGrant, TeamMember,
                               TeamProject, TeamResourceLease)

TEAM_ID = 1
# Issuer recorded for members an operator verified against a Brain credential
# rather than an external identity provider. The stored issuer/subject describe
# how the operator verified the person; they are not evidence of OIDC.
CREDENTIAL_ISSUER = "brain-credential"
# Issuer for members verified against the web UI login (approver subject ``web:<user>``).
WEB_LOGIN_ISSUER = "brain-web-login"
ADMIN_SCOPE = "team:admin"
# What a member's own dashboard and MCP client read, plus recording decisions and rules, queuing
# jobs and recording harness work (task observations, lessons, handoffs). Project roles still decide
# every repository (a job needs the run.request permission on its repository; one without a repository
# needs a team administrator; a lesson or observation needs knowledge.propose on its task's repository,
# so Readers are still refused); kept equal to DASHBOARD_SCOPES in apps/web/lib/team-scopes.ts.
MEMBER_CORE_SCOPES = frozenset({
    "control:read", "core:write", "coupling:read", "drift:read", "freshness:read", "graph:read", "harness:read",
    "harness:write", "impact:read", "jobs:write", "ledger:read", "operations:read", "portfolio:read", "setup:read",
    "workspace:read",
})
# Extra for team admins: run the Team page (members, roles), issue member keys, and use the
# administrative dashboard screens (Setup, Settings, Access, job retries). Those routes also require
# a live team administrator, so the scopes alone grant nothing to a member who is not one.
ADMIN_EXTRA_SCOPES = frozenset({"operations:write", "principals:read", "principals:write", "setup:write", ADMIN_SCOPE})


class TeamNotFound(LookupError):
    """A referenced team, principal, project or repository does not exist."""


def member_scopes(is_admin: bool = False) -> list[str]:
    """Scopes a human member credential needs (``has_scope`` has no prefix wildcard).

    The same set the web Team page mints (``memberKeyScopes``), so a key from the CLI or an
    invitation opens the dashboard and the MCP resources as well as the ``team:*`` routes."""
    scopes = {f"team:{permission}" for role in ROLE_PERMISSIONS.values() for permission in role}
    scopes |= MEMBER_CORE_SCOPES
    if is_admin:
        scopes |= ADMIN_EXTRA_SCOPES
    return sorted(scopes)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


async def _record_membership(session: AsyncSession, kind: str, summary: str, *, actor_id: int | None,
                             principal_id: int, project_id: uuid.UUID | None = None, **details) -> None:
    """Team-administration activity (read back by team administrators in the activity feed).

    ``actor_id`` is the administrator who made the change; ``None`` marks an
    operator action on the server (the CLI), which has no principal."""
    await record_event(session, kind=kind, summary=summary, audience="admin", actor_principal_id=actor_id,
                       actor_label=None if actor_id else "operator", subject_principal_id=principal_id,
                       project_id=project_id, details={"principal_id": principal_id, **details})


async def record_sso_event(session: AsyncSession, kind: str, principal: Principal, issuer: str, *,
                           actor_id: int | None) -> None:
    """Record a single sign-on binding change (``sso.bound`` / ``sso.unbound``) for team administrators."""
    change = "can now sign in" if kind == "sso.bound" else "can no longer sign in"
    await _record_membership(session, kind, f"{principal.name} {change} with single sign-on",
                             actor_id=actor_id, principal_id=principal.id, issuer=issuer)


def team_dict(team: Team) -> dict:
    return {"id": team.id, "name": team.name}


def member_dict(member: TeamMember, principal: Principal | None = None) -> dict:
    return {"principal_id": member.principal_id, "principal_name": principal.name if principal else None,
            "team_id": member.team_id, "verification_issuer": member.verification_issuer,
            "verification_subject": member.verification_subject, "verified_at": _iso(member.verified_at),
            "is_admin": bool(member.is_admin), "revoked_at": _iso(member.revoked_at)}


def project_dict(project: TeamProject) -> dict:
    return {"id": str(project.id), "team_id": project.team_id, "name": project.name,
            "permissions": list(project.permissions or []), "version": project.version,
            "disabled_at": _iso(project.disabled_at)}


def project_member_dict(membership: ProjectMember) -> dict:
    return {"project_id": str(membership.project_id), "principal_id": membership.principal_id,
            "role": membership.role, "version": membership.version, "revoked_at": _iso(membership.revoked_at)}


def repository_dict(link: ProjectRepository, repository: Repository | None = None) -> dict:
    return {"project_id": str(link.project_id), "repository_id": link.repository_id,
            "path": repository.path if repository else None}


async def get_team(session: AsyncSession, team_id: int = TEAM_ID) -> Team | None:
    return await fresh(session, Team, Team.id == team_id)


async def require_team(session: AsyncSession, team_id: int = TEAM_ID) -> Team:
    team = await get_team(session, team_id)
    if team is None:
        raise TeamNotFound("Team is not initialized; run `brain team init` first")
    return team


async def init_team(session: AsyncSession, name: str) -> Team:
    """Create the single team, or return it when it already exists under ``name``."""
    name = name.strip()
    if not name:
        raise TeamConflict("Team name must not be blank")
    team = await fresh(session, Team, Team.id == TEAM_ID, exclusive=True)
    if team is None:
        try:
            async with session.begin_nested():
                team = Team(id=TEAM_ID, name=name)
                session.add(team)
                await session.flush()
        except IntegrityError:
            team = await fresh(session, Team, Team.id == TEAM_ID)
            if team is None:
                raise
    if team.name != name:
        raise TeamConflict(f"Team already initialized as {team.name!r}")
    return team


def _is_identifier(value: int | str) -> bool:
    return isinstance(value, int) or (isinstance(value, str) and value.isdigit())


async def resolve_principal(session: AsyncSession, principal: int | str) -> Principal:
    if _is_identifier(principal):
        found = await fresh(session, Principal, Principal.id == int(principal))
    else:
        found = await fresh(session, Principal, Principal.name == principal)
    if found is None:
        raise TeamNotFound(f"Principal {principal!r} not found")
    return found


async def resolve_principal_name(session: AsyncSession, name: str) -> Principal:
    """Resolve a principal explicitly by name, including all-digit names."""
    found = await fresh(session, Principal, Principal.name == name)
    if found is None:
        raise TeamNotFound(f"Principal {name!r} not found")
    return found


async def create_human_principal(session: AsyncSession, name: str, *, is_admin: bool = False) -> tuple[str, Principal]:
    """Mint (or extend) a human principal with a member credential; the plaintext key is returned once."""
    existing = (await session.execute(select(Principal).where(Principal.name == name))).scalar_one_or_none()
    if existing is not None and existing.kind != "human":
        raise TeamConflict(f"Principal {name!r} exists with kind {existing.kind!r}; team members must be human")
    raw, credential = await mint_credential(session, name=name, scopes=member_scopes(is_admin), kind="human")
    principal = await fresh(session, Principal, Principal.id == credential.principal_id)
    return raw, principal


async def add_member(session: AsyncSession, *, principal: Principal, issuer: str, subject: str,
                     is_admin: bool | None = None, team_id: int = TEAM_ID,
                     actor_id: int | None = None) -> TeamMember:
    """Record verified membership for a human principal (idempotent; reactivates a revoked row)."""
    issuer, subject = issuer.strip(), subject.strip()
    if not issuer or not subject:
        raise TeamConflict("Verification issuer and subject must not be blank")
    if principal.kind != "human":
        raise TeamConflict("Team members must be human principals")
    if principal.disabled_at:
        raise TeamConflict("Principal is disabled")
    await require_team(session, team_id)
    claimed = await fresh(session, TeamMember, TeamMember.verification_issuer == issuer,
                          TeamMember.verification_subject == subject)
    if claimed is not None and claimed.principal_id != principal.id:
        raise TeamConflict("Verification identity already belongs to another member")
    member = await fresh(session, TeamMember, TeamMember.principal_id == principal.id, exclusive=True)
    was_live = member is not None and member.revoked_at is None and bool(member.verified_at)
    was_admin = bool(member.is_admin) if member is not None else False
    if member is None:
        try:
            async with session.begin_nested():
                member = TeamMember(principal_id=principal.id, team_id=team_id, verification_issuer=issuer,
                                    verification_subject=subject, verified_at=_now(), is_admin=is_admin)
                session.add(member)
                await session.flush()
        except IntegrityError:
            member = await fresh(session, TeamMember, TeamMember.principal_id == principal.id)
            if member is None:
                raise
    if member.team_id != team_id:
        raise TeamConflict("Principal belongs to a different team")
    if (member.verification_issuer, member.verification_subject) != (issuer, subject):
        raise TeamConflict("Member is verified under a different issuer/subject")
    if is_admin is not None:
        member.is_admin = is_admin
    if member.revoked_at or not member.verified_at:
        member.revoked_at = None
        member.verified_at = _now()
    await session.flush()
    if not was_live:
        await _record_membership(session, "membership.added", f"{principal.name} joined the team",
                                 actor_id=actor_id, principal_id=principal.id, is_admin=bool(member.is_admin))
    elif bool(member.is_admin) != was_admin:
        granted = bool(member.is_admin)
        change = "became" if granted else "is no longer"
        await _record_membership(session, "membership.admin_granted" if granted else "membership.admin_revoked",
                                 f"{principal.name} {change} a team administrator",
                                 actor_id=actor_id, principal_id=principal.id, is_admin=granted)
    return member


async def approver_subjects(session: AsyncSession, principal: Principal) -> set[str]:
    """The consent subjects (``OAuthRefreshToken.subject``) under which ``principal`` approves OAuth clients.

    A key approval records the key's principal name; a web-login approval
    records ``web:<user>``, which is this principal only when its membership
    is verified under the web login (see ``brain/team/oauth_link.py``)."""
    subjects = {principal.name}
    member = (await session.execute(select(TeamMember).where(TeamMember.principal_id == principal.id))
              ).scalar_one_or_none()
    if member is not None and member.verification_issuer == WEB_LOGIN_ISSUER and member.verification_subject:
        subjects.add(f"web:{member.verification_subject}")
    return subjects


async def revoke_oauth_approvals(session: AsyncSession, principal: Principal, now: datetime | None = None) -> None:
    """Revoke every OAuth grant ``principal`` approved or holds: refresh tokens and their access credentials.

    Personal-scope tokens live on a per-client service principal
    (``oauth:<client>``), not on the approver, so they are matched by the
    consent subject recorded on the refresh row. Team-scope tokens live on the
    member's own principal and are matched by principal name."""
    now = now or _now()
    linked = or_(OAuthRefreshToken.principal_name == principal.name,
                 OAuthRefreshToken.subject.in_(sorted(await approver_subjects(session, principal))))
    await session.execute(update(ApiCredential).where(
        ApiCredential.revoked_at.is_(None),
        ApiCredential.id.in_(select(OAuthRefreshToken.access_credential_id).where(
            linked, OAuthRefreshToken.access_credential_id.is_not(None))))
        .values(revoked_at=now))
    await session.execute(update(OAuthRefreshToken).where(OAuthRefreshToken.revoked_at.is_(None), linked)
                          .values(revoked_at=now))


async def revoke_principal_access(session: AsyncSession, principal: Principal) -> None:
    """Revoke every credential, OAuth refresh token and agent run that acts for ``principal``.

    Membership rows alone do not stop a key: a revoked member's credential
    would otherwise stay live for solo surfaces and for any later fallback.
    This revokes the principal's own credentials (named keys and OAuth access
    tokens), the OAuth grants it approved (``revoke_oauth_approvals``: personal
    clients too, whose tokens live on a service principal), and the runs it
    initiated together with their run credentials, in the caller's transaction.
    Execution grants on every project are revoked too, so a re-added member
    needs a fresh grant. Coordination leases held by the principal or by its
    runs are released, and their fencing tokens advance."""
    now = _now()
    await session.execute(update(TeamResourceLease).where(
        TeamResourceLease.released_at.is_(None),
        or_(TeamResourceLease.holder_id == principal.id,
            TeamResourceLease.holder_id.in_(select(AgentRun.principal_id).where(AgentRun.initiator_id == principal.id))))
        .values(released_at=now, released_reason="holder revoked", fencing_token=TeamResourceLease.fencing_token + 1))
    await session.execute(update(TeamExecutionGrant).where(
        TeamExecutionGrant.principal_id == principal.id, TeamExecutionGrant.revoked_at.is_(None))
        .values(revoked_at=now, revoked_by="system:member-revoked", version=TeamExecutionGrant.version + 1))
    credential_ids = select(ApiCredential.id).where(ApiCredential.principal_id == principal.id)
    runs = (await session.execute(select(AgentRun).where(AgentRun.initiator_id == principal.id,
                                                         AgentRun.revoked_at.is_(None))
                                  .with_for_update())).scalars().all()
    run_credentials = [run.credential_id for run in runs]
    for run in runs:
        run.revoked_at = now
    await session.execute(update(OAuthRefreshToken).where(
        OAuthRefreshToken.revoked_at.is_(None),
        OAuthRefreshToken.access_credential_id.in_(credential_ids)).values(revoked_at=now))
    await revoke_oauth_approvals(session, principal, now)
    await session.execute(update(ApiCredential).where(
        ApiCredential.revoked_at.is_(None),
        or_(ApiCredential.principal_id == principal.id, ApiCredential.id.in_(run_credentials)))
        .values(revoked_at=now))
    await session.flush()


async def lock_principal(session: AsyncSession, principal_id: int) -> Principal | None:
    """Lock a principal row ``FOR NO KEY UPDATE``: the first lock of anything that revokes it.

    Every request of that principal starts by taking the row ``FOR SHARE``
    (``live_credential``), so this waits for requests in flight and holds new
    ones back before they lock anything else. ``NO KEY`` keeps foreign-key
    inserts that reference the principal (activity events, proposals) unblocked."""
    return (await session.execute(select(Principal).where(Principal.id == principal_id)
                                  .with_for_update(key_share=True)
                                  .execution_options(populate_existing=True))).scalar_one_or_none()


async def revoke_member(session: AsyncSession, *, principal: Principal, actor_id: int | None = None) -> TeamMember:
    """Revoke team membership, every live project role and all access acting for the principal.

    The principal's pending memory proposals are withdrawn with the reason
    ``Author access revoked`` (brain/memory/proposals.py): nobody is left to
    answer review questions about them, and a reviewer approving one later
    would publish knowledge on behalf of someone who no longer has access.
    Knowledge already approved stays.

    Locks follow the team lock order (``brain/team/authorization.py``): the
    principals of the runs the member initiated (newest first, as a run
    request walks its delegation chain), the member's own principal, those
    runs, then the membership row. A request of the member or of one of its
    runs takes the same rows in the same order, so revocation waits for it
    instead of deadlocking with it, and once this transaction commits every
    credential acting for the member is revoked. A deadlock victim attempt is
    rolled back to a savepoint and retried a bounded number of times
    (``retry_transient``); if every attempt fails the error propagates and
    nothing is revoked."""
    principal_id, principal_name = principal.id, principal.name

    async def attempt() -> TeamMember:
        run_principals = (await session.execute(
            select(AgentRun.principal_id).where(AgentRun.initiator_id == principal_id, AgentRun.revoked_at.is_(None))
            .order_by(AgentRun.principal_id.desc()))).scalars().all()
        for run_principal in run_principals:
            await lock_principal(session, run_principal)
        identity = await lock_principal(session, principal_id)
        if identity is None:
            raise TeamNotFound("Principal is not a team member")
        await session.execute(select(AgentRun.id).where(AgentRun.initiator_id == principal_id,
                                                        AgentRun.revoked_at.is_(None)).with_for_update())
        member = await fresh(session, TeamMember, TeamMember.principal_id == principal_id, exclusive=True)
        if member is None:
            raise TeamNotFound("Principal is not a team member")
        if member.revoked_at is None:
            member.revoked_at = _now()
            await _record_membership(session, "membership.revoked", f"{principal_name} was removed from the team",
                                     actor_id=actor_id, principal_id=principal_id)
        rows = (await session.execute(select(ProjectMember).where(ProjectMember.principal_id == principal_id,
                                                                  ProjectMember.revoked_at.is_(None))
                                      .with_for_update())).scalars().all()
        for membership in rows:
            membership.revoked_at = member.revoked_at
            membership.version += 1
        await revoke_principal_access(session, identity)
        await withdraw_author_proposals(session, principal_id)
        return member

    return await retry_transient(session, attempt)


async def dissolve_team(session: AsyncSession, team_id: int = TEAM_ID) -> Team:
    """Return a team whose members are all revoked to solo (operator-only).

    Solo makes every authenticated key the implicit administrator, so the
    switch is never automatic: ``team_state`` stays ``"team"`` after the last
    revocation until this records ``dissolved_at``. Refused while any verified
    member is live. Adding a member afterwards turns the team back on.

    Solo authority goes to every live credential, so the OAuth grants former
    members approved are revoked again here (``revoke_oauth_approvals``): a
    personal client a member approved must not come back as the owner. Other
    credentials are left alone; the owner's own keys become the owner's."""
    team = await fresh(session, Team, Team.id == team_id, exclusive=True)
    if team is None:
        raise TeamNotFound("Team is not initialized; run `brain team init` first")
    live = (await session.execute(select(TeamMember.principal_id).where(
        TeamMember.team_id == team_id, TeamMember.revoked_at.is_(None), TeamMember.verified_at.is_not(None))
        .limit(1))).scalar_one_or_none()
    if live is not None:
        raise TeamConflict("Revoke every team member before dissolving the team")
    former = (await session.execute(select(Principal).join(TeamMember, TeamMember.principal_id == Principal.id)
                                    .where(TeamMember.team_id == team_id))).scalars().all()
    for principal in former:
        await revoke_oauth_approvals(session, principal)
    team.dissolved_at = _now()
    await session.flush()
    return team


async def list_members(session: AsyncSession, team_id: int = TEAM_ID) -> list[dict]:
    rows = (await session.execute(select(TeamMember, Principal).join(Principal, Principal.id == TeamMember.principal_id)
                                  .where(TeamMember.team_id == team_id).order_by(TeamMember.principal_id))).all()
    return [member_dict(member, principal) for member, principal in rows]


async def create_project(session: AsyncSession, *, name: str, allowed: list[str] | None = None,
                         team_id: int = TEAM_ID) -> TeamProject:
    """Create a project by name, or return the existing one when its policy is identical."""
    name = name.strip()
    if not name:
        raise TeamConflict("Project name must not be blank")
    try:
        policy = sorted(permissions(allowed)) if allowed is not None else sorted(Permission)
    except TeamDenied as exc:
        raise TeamConflict(str(exc)) from exc
    await require_team(session, team_id)
    project = await fresh(session, TeamProject, TeamProject.team_id == team_id, TeamProject.name == name,
                          exclusive=True)
    if project is None:
        try:
            async with session.begin_nested():
                project = TeamProject(team_id=team_id, name=name, permissions=policy)
                session.add(project)
                await session.flush()
        except IntegrityError:
            project = await fresh(session, TeamProject, TeamProject.team_id == team_id, TeamProject.name == name)
            if project is None:
                raise
    if sorted(project.permissions or []) != policy:
        raise TeamConflict(f"Project {name!r} already exists with a different permission policy")
    return project


async def resolve_project(session: AsyncSession, project: uuid.UUID | str, team_id: int = TEAM_ID) -> TeamProject:
    if isinstance(project, uuid.UUID):
        found = await fresh(session, TeamProject, TeamProject.id == project)
    else:
        try:
            found = await fresh(session, TeamProject, TeamProject.id == uuid.UUID(project))
        except ValueError:
            found = None
        if found is None:
            found = await fresh(session, TeamProject, TeamProject.team_id == team_id, TeamProject.name == project)
    if found is None:
        raise TeamNotFound(f"Project {project!r} not found")
    return found


async def list_projects(session: AsyncSession, team_id: int = TEAM_ID) -> list[dict]:
    rows = (await session.execute(select(TeamProject).where(TeamProject.team_id == team_id)
                                  .order_by(TeamProject.name))).scalars().all()
    return [project_dict(project) for project in rows]


async def assign_project_member(session: AsyncSession, *, project_id: uuid.UUID, principal_id: int,
                                role: str, actor_id: int | None = None) -> ProjectMember:
    """Grant or change a project role; a no-op when the live role already matches."""
    if role not in ROLE_PERMISSIONS:
        raise TeamConflict("Unknown project role")
    project = await fresh(session, TeamProject, TeamProject.id == project_id, exclusive=True)
    if project is None:
        raise TeamNotFound("Project not found")
    member = await fresh(session, TeamMember, TeamMember.principal_id == principal_id,
                         TeamMember.team_id == project.team_id)
    if member is None or member.revoked_at or not member.verified_at:
        raise TeamConflict("Principal is not a verified, active team member")
    membership = await fresh(session, ProjectMember, ProjectMember.project_id == project_id,
                             ProjectMember.principal_id == principal_id, exclusive=True)
    if membership is None:
        try:
            async with session.begin_nested():
                membership = ProjectMember(project_id=project_id, principal_id=principal_id, role=role)
                session.add(membership)
                await session.flush()
            await _record_membership(session, "project_role.granted",
                                     f"{role.capitalize()} role in {project.name}",
                                     actor_id=actor_id, principal_id=principal_id, project_id=project_id, role=role)
            return membership
        except IntegrityError:
            membership = await fresh(session, ProjectMember, ProjectMember.project_id == project_id,
                                     ProjectMember.principal_id == principal_id)
            if membership is None:
                raise
    if membership.role != role or membership.revoked_at is not None:
        previous = None if membership.revoked_at is not None else membership.role
        membership.role = role
        membership.revoked_at = None
        membership.version += 1
        await session.flush()
        await _record_membership(session, "project_role.changed" if previous else "project_role.granted",
                                 f"{role.capitalize()} role in {project.name}",
                                 actor_id=actor_id, principal_id=principal_id, project_id=project_id, role=role,
                                 previous_role=previous)
    return membership


async def revoke_project_member(session: AsyncSession, *, project_id: uuid.UUID, principal_id: int,
                                actor_id: int | None = None) -> ProjectMember:
    membership = await fresh(session, ProjectMember, ProjectMember.project_id == project_id,
                             ProjectMember.principal_id == principal_id, exclusive=True)
    if membership is None:
        raise TeamNotFound("Project membership not found")
    if membership.revoked_at is None:
        membership.revoked_at = _now()
        membership.version += 1
        await session.execute(update(TeamExecutionGrant).where(
            TeamExecutionGrant.project_id == project_id, TeamExecutionGrant.principal_id == principal_id,
            TeamExecutionGrant.revoked_at.is_(None))
            .values(revoked_at=membership.revoked_at, revoked_by="system:project-member-revoked",
                    version=TeamExecutionGrant.version + 1))
        await session.flush()
        await _record_membership(session, "project_role.revoked",
                                 f"{membership.role.capitalize()} role revoked",
                                 actor_id=actor_id, principal_id=principal_id, project_id=project_id,
                                 role=membership.role)
    return membership


async def list_project_members(session: AsyncSession, project_id: uuid.UUID) -> list[dict]:
    rows = (await session.execute(select(ProjectMember).where(ProjectMember.project_id == project_id)
                                  .order_by(ProjectMember.principal_id))).scalars().all()
    return [project_member_dict(membership) for membership in rows]


async def resolve_repository(session: AsyncSession, repository: int | str) -> Repository:
    if _is_identifier(repository):
        found = await fresh(session, Repository, Repository.id == int(repository))
    else:
        found = await fresh(session, Repository, Repository.path == repository)
    if found is None:
        raise TeamNotFound(f"Repository {repository!r} not found; index it first")
    return found


async def attach_repository(session: AsyncSession, *, project_id: uuid.UUID,
                            repository: Repository) -> ProjectRepository:
    """Bind an indexed repository to exactly one project (idempotent for the same project)."""
    project = await fresh(session, TeamProject, TeamProject.id == project_id)
    if project is None:
        raise TeamNotFound("Project not found")
    link = await fresh(session, ProjectRepository, ProjectRepository.repository_id == repository.id, exclusive=True)
    if link is None:
        try:
            async with session.begin_nested():
                link = ProjectRepository(repository_id=repository.id, project_id=project_id)
                session.add(link)
                await session.flush()
                return link
        except IntegrityError:
            link = await fresh(session, ProjectRepository, ProjectRepository.repository_id == repository.id)
            if link is None:
                raise
    if link.project_id != project_id:
        raise TeamConflict("Repository is already attached to another project")
    return link


async def list_project_repositories(session: AsyncSession, project_id: uuid.UUID) -> list[dict]:
    rows = (await session.execute(select(ProjectRepository, Repository)
                                  .join(Repository, Repository.id == ProjectRepository.repository_id)
                                  .where(ProjectRepository.project_id == project_id)
                                  .order_by(ProjectRepository.repository_id))).all()
    return [repository_dict(link, repository) for link, repository in rows]
