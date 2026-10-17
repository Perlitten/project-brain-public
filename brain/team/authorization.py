from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal
import uuid

from loguru import logger
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from brain.auth.principals import ResolvedPrincipal
from brain.config.settings import settings
from brain.database.models import ApiCredential, Principal
from brain.database.harness_models import AgentTask
from brain.team.models import (AgentRun, ProjectMember, ProjectRepository, Team, TeamMember, TeamProject,
                               TeamTaskBinding)
from brain.team.restore_fence import FENCED, access_fenced


class Permission(StrEnum):
    READ = "read"
    CREATE_TASK = "task.create"
    REQUEST_RUN = "run.request"
    PROPOSE = "knowledge.propose"
    APPROVE = "knowledge.approve"
    ACCEPT = "task.accept"
    EXECUTE = "execute"
    MERGE = "merge"
    TRANSFER = "provider.transfer"


ROLE_PERMISSIONS = {
    "reader": frozenset({Permission.READ}),
    "contributor": frozenset({Permission.READ, Permission.CREATE_TASK, Permission.REQUEST_RUN, Permission.PROPOSE}),
    "reviewer": frozenset({Permission.READ, Permission.CREATE_TASK, Permission.REQUEST_RUN,
                           Permission.PROPOSE, Permission.APPROVE, Permission.ACCEPT}),
}
RUN_CAPABILITIES = frozenset({Permission.READ, Permission.REQUEST_RUN})


class TeamDenied(PermissionError):
    pass


class TeamConflict(ValueError):
    pass


TeamState = Literal["solo", "team"]
FULL_AUTHORITY = frozenset(Permission)
_strict_flag_warned = False


def require_team_storage() -> None:
    """Team tables exist only under full storage; Lite keeps the personal surfaces alone."""
    if settings.BRAIN_STORAGE_MODE != "full":
        raise TeamDenied("Team foundations require full storage")


def strict_team_mode() -> bool:
    """Deprecated ``BRAIN_TEAM_MODE`` alias: act as a team before the first member joins.

    Boundaries now switch on with the first live team member. The flag only
    keeps rejecting the shared API key and dev-open access on an empty team,
    for operators who provision members offline before opening the API."""
    global _strict_flag_warned
    if settings.BRAIN_TEAM_MODE and not _strict_flag_warned:
        _strict_flag_warned = True
        logger.warning("BRAIN_TEAM_MODE is deprecated: team boundaries now switch on with the first team member; "
                       "the flag only rejects the shared API key and dev-open access before that")
    return bool(settings.BRAIN_TEAM_MODE)


async def team_state(session: AsyncSession, team_id: int = 1) -> TeamState:
    """``"solo"`` until the team has a verified member, ``"team"`` from then on.

    Solo is a team of one: every authenticated caller is the implicit
    administrator of every repository. Team routes repository access through
    project membership (see ``authorize_repository``). Revoking every member
    does not fall back to solo: a team that ever had a verified member stays
    ``"team"`` until an operator dissolves it (``brain team dissolve`` sets
    ``teams.dissolved_at``), and only members revoked after that moment count
    again. A Brain restored from a team snapshot is ``"team"`` until
    reconciled, even if no member is live (``brain.team.restore_fence``)."""
    if settings.BRAIN_STORAGE_MODE != "full":
        return "solo"
    if strict_team_mode() or await access_fenced(session):
        return "team"
    dissolved = select(Team.dissolved_at).where(Team.id == team_id).scalar_subquery()
    member = (await session.execute(select(TeamMember.principal_id).where(
        TeamMember.team_id == team_id, TeamMember.verified_at.is_not(None),
        or_(TeamMember.revoked_at.is_(None), dissolved.is_(None), TeamMember.revoked_at > dissolved))
        .limit(1))).scalar_one_or_none()
    return "team" if member is not None else "solo"


def permissions(value: list[str]) -> frozenset[Permission]:
    try:
        return frozenset(Permission(item) for item in value)
    except (ValueError, TypeError) as exc:
        raise TeamDenied("Invalid authority policy") from exc


async def fresh(session: AsyncSession, model, *predicates, exclusive: bool = False):
    return (await session.execute(select(model).where(*predicates)
                                  .with_for_update(read=not exclusive)
                                  .execution_options(populate_existing=True))).scalar_one_or_none()


async def live_credential(session: AsyncSession, principal_id: int, credential_id: int | None) -> ResolvedPrincipal:
    if principal_id <= 0 or credential_id is None:
        raise TeamDenied("A named live credential is required")
    if await access_fenced(session):
        raise TeamDenied(FENCED)
    identity = await fresh(session, Principal, Principal.id == principal_id)
    credential = await fresh(session, ApiCredential, ApiCredential.id == credential_id)
    now = datetime.now(timezone.utc)
    if (identity is None or credential is None or credential.principal_id != principal_id
            or identity.disabled_at or credential.revoked_at
            or (credential.expires_at is not None and credential.expires_at <= now)):
        raise TeamDenied("Credential is no longer authorized")
    return ResolvedPrincipal(id=identity.id, name=identity.name, kind=identity.kind,
                             credential_id=credential.id, scopes=frozenset(credential.scopes or []))


async def human_member(session: AsyncSession, caller: ResolvedPrincipal, team_id: int) -> TeamMember:
    member = await fresh(session, TeamMember, TeamMember.principal_id == caller.id, TeamMember.team_id == team_id)
    if (caller.kind != "human" or member is None or not member.verified_at or member.revoked_at
            or not member.verification_issuer.strip() or not member.verification_subject.strip()):
        raise TeamDenied("Verified team membership is required")
    return member


@dataclass(frozen=True)
class Authority:
    actor_id: int
    initiator_id: int
    initiator_credential_id: int | None
    allowed: frozenset[Permission]
    run_id: uuid.UUID | None = None
    team_state: TeamState = "team"


async def authorize(session: AsyncSession, caller: ResolvedPrincipal, project_id: uuid.UUID,
                    permission: Permission, *, task_id: uuid.UUID | None = None,
                    author_id: int | None = None) -> Authority:
    require_team_storage()
    caller = await live_credential(session, caller.id, caller.credential_id)
    project = await fresh(session, TeamProject, TeamProject.id == project_id)
    if project is None or project.disabled_at:
        raise TeamDenied("Project access denied")
    chain: list[AgentRun] = []
    initiator = caller
    if caller.kind == "run":
        run = await fresh(session, AgentRun, AgentRun.principal_id == caller.id)
        if run is None or run.credential_id != caller.credential_id or (task_id and run.task_id != task_id):
            raise TeamDenied("Run identity mismatch")
        task_id = run.task_id
        while run is not None:
            if len(chain) >= 32 or run.id in {item.id for item in chain} or run.revoked_at or run.task_id != task_id:
                raise TeamDenied("Invalid delegation chain")
            await live_credential(session, run.principal_id, run.credential_id)
            chain.append(run)
            run = await fresh(session, AgentRun, AgentRun.id == run.parent_id) if run.parent_id else None
            if chain[-1].parent_id and run is None:
                raise TeamDenied("Missing parent authority")
        initiator = await live_credential(session, chain[0].initiator_id, chain[0].initiator_credential_id)
        if any(r.initiator_id != initiator.id or r.initiator_credential_id != initiator.credential_id for r in chain):
            raise TeamDenied("Delegation changed initiator")
    await human_member(session, initiator, project.team_id)
    membership = await fresh(session, ProjectMember, ProjectMember.project_id == project_id,
                             ProjectMember.principal_id == initiator.id)
    if membership is None or membership.revoked_at:
        raise TeamDenied("Project access denied")
    allowed = ROLE_PERMISSIONS.get(membership.role, frozenset()) & permissions(project.permissions)
    allowed = frozenset(p for p in allowed if initiator.has_scope(f"team:{p}") and caller.has_scope(f"team:{p}"))
    if task_id is not None:
        binding = await fresh(session, TeamTaskBinding, TeamTaskBinding.task_id == task_id,
                              TeamTaskBinding.project_id == project_id)
        if binding is None:
            raise TeamDenied("Task access denied")
        repository = await fresh(session, ProjectRepository, ProjectRepository.repository_id == binding.repository_id,
                                 ProjectRepository.project_id == project_id)
        if repository is None:
            raise TeamDenied("Source repository no longer belongs to this project")
        allowed &= permissions(binding.permissions)
    for grant in chain:
        grant_credential = await live_credential(session, grant.principal_id, grant.credential_id)
        allowed &= permissions(grant.permissions) & permissions(grant.executor_capabilities) & RUN_CAPABILITIES
        allowed = frozenset(p for p in allowed if grant_credential.has_scope(f"team:{p}"))
    if permission == Permission.APPROVE and (author_id is None or author_id == initiator.id):
        raise TeamDenied("Independent, attributed approval is required")
    if permission not in allowed:
        raise TeamDenied("Permission denied")
    if task_id is not None and permission == Permission.REQUEST_RUN:
        task = await fresh(session, AgentTask, AgentTask.id == task_id)
        if task is None or task.cancellation_requested_at is not None or task.status in {"cancelled", "completed"}:
            raise TeamDenied("Task cannot accept further run requests")
    assert initiator.credential_id is not None
    return Authority(caller.id, initiator.id, initiator.credential_id, allowed, chain[0].id if chain else None)


async def change_role(session: AsyncSession, caller: ResolvedPrincipal, project_id: uuid.UUID,
                      principal_id: int, role: str, expected_version: int) -> None:
    await administrator(session, caller, project_id)
    if role not in ROLE_PERMISSIONS:
        raise TeamConflict("Unknown project role")
    changed = await session.execute(update(ProjectMember).where(
        ProjectMember.project_id == project_id, ProjectMember.principal_id == principal_id,
        ProjectMember.version == expected_version).values(role=role, version=expected_version + 1)
        .returning(ProjectMember.principal_id))
    if changed.scalar_one_or_none() is None:
        raise TeamConflict("Membership version conflict")


async def administrator(session: AsyncSession, caller: ResolvedPrincipal, project_id: uuid.UUID) -> None:
    require_team_storage()
    caller = await live_credential(session, caller.id, caller.credential_id)
    project = await fresh(session, TeamProject, TeamProject.id == project_id, exclusive=True)
    if project is None:
        raise TeamDenied("Project access denied")
    member = await human_member(session, caller, project.team_id)
    if not member.is_admin or not caller.has_scope("team:admin"):
        raise TeamDenied("Administration permission required")


async def change_policy(session: AsyncSession, caller: ResolvedPrincipal, project_id: uuid.UUID,
                        allowed: list[str], expected_version: int) -> None:
    await administrator(session, caller, project_id)
    policy = sorted(permissions(allowed))
    changed = await session.execute(update(TeamProject).where(
        TeamProject.id == project_id, TeamProject.version == expected_version)
        .values(permissions=policy, version=expected_version + 1).returning(TeamProject.id))
    if changed.scalar_one_or_none() is None:
        raise TeamConflict("Policy version conflict")


async def team_administrator(session: AsyncSession, caller: ResolvedPrincipal, team_id: int = 1) -> TeamMember:
    """Team-level administration: a live human member flagged is_admin holding the team:admin scope.

    Unlike ``administrator`` this does not require a project, so it guards the
    provisioning operations (projects, team membership) that create the objects
    project-scoped checks would otherwise need."""
    require_team_storage()
    caller = await live_credential(session, caller.id, caller.credential_id)
    member = await human_member(session, caller, team_id)
    if not member.is_admin or not caller.has_scope("team:admin"):
        raise TeamDenied("Administration permission required")
    return member


def solo_authority(caller: ResolvedPrincipal) -> Authority:
    """The implicit administrator of a team of one: full authority, no credential checks."""
    return Authority(caller.id, caller.id, caller.credential_id, FULL_AUTHORITY, team_state="solo")


async def authorize_repository(session: AsyncSession, caller: ResolvedPrincipal, repository_id: int | None,
                               permission: Permission) -> Authority:
    """Authority over one indexed repository; ``None`` is the unscoped, every-repository surface.

    Solo state short-circuits to ``solo_authority`` for any authenticated
    caller, including the shared legacy key and dev-open access. In team state
    the caller needs a live named credential; a repository attached to a
    project is checked through ``authorize`` with the member's project role,
    while an unattached repository or an unscoped call requires a team
    administrator and grants full authority."""
    if await team_state(session) == "solo":
        return solo_authority(caller)
    live = await live_credential(session, caller.id, caller.credential_id)
    link = None
    if repository_id is not None:
        link = await fresh(session, ProjectRepository, ProjectRepository.repository_id == repository_id)
    if link is None:
        member = await team_administrator(session, live)
        return Authority(live.id, member.principal_id, live.credential_id, FULL_AUTHORITY)
    return await authorize(session, live, link.project_id, permission)
