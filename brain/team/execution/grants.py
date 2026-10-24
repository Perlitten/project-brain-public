"""Execution grants and LLM consent/budget administration."""
from __future__ import annotations

import re
import uuid
from typing import Any, Mapping
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from brain.auth.principals import ResolvedPrincipal
from brain.team import execution_llm
from brain.team._util import _iso, _now
from brain.team.activity import record_event
from brain.team.authorization import Permission, TeamConflict, administrator, authorize, fresh, require_team_storage
from brain.team.models import ProjectMember, TeamExecutionGrant, TeamMember, TeamProject
from brain.team.execution.policy import EXECUTION_ROLES, operator_policy


def grant_dict(grant: TeamExecutionGrant) -> dict:
    return {"project_id": str(grant.project_id), "principal_id": grant.principal_id, "granted_by": grant.granted_by,
            "granted_at": _iso(grant.granted_at), "revoked_at": _iso(grant.revoked_at),
            "revoked_by": grant.revoked_by, "version": grant.version, "active": grant.revoked_at is None}


async def set_grant(session: AsyncSession, project_id: uuid.UUID, principal_id: int,
                    granted_by: str) -> TeamExecutionGrant:
    """Record (or renew) an execution grant; only for a live verified member with a write-capable role."""
    from brain.team.provisioning import TeamNotFound

    require_team_storage()
    project = await fresh(session, TeamProject, TeamProject.id == project_id)
    if project is None:
        raise TeamNotFound("Project not found")
    member = await fresh(session, TeamMember, TeamMember.principal_id == principal_id,
                         TeamMember.team_id == project.team_id)
    if member is None or member.verified_at is None or member.revoked_at is not None:
        raise TeamConflict("Execution grants need a verified, live team member")
    membership = await fresh(session, ProjectMember, ProjectMember.project_id == project_id,
                             ProjectMember.principal_id == principal_id)
    if membership is None or membership.revoked_at is not None or membership.role not in EXECUTION_ROLES:
        raise TeamConflict("Execution grants need a Contributor or Reviewer role on the project")
    grant = await fresh(session, TeamExecutionGrant, TeamExecutionGrant.project_id == project_id,
                        TeamExecutionGrant.principal_id == principal_id, exclusive=True)
    if grant is None:
        grant = TeamExecutionGrant(project_id=project_id, principal_id=principal_id, granted_by=granted_by[:128],
                                   granted_at=_now(), version=1)
        session.add(grant)
    elif grant.revoked_at is not None:
        grant.revoked_at, grant.revoked_by = None, None
        grant.granted_by, grant.granted_at = granted_by[:128], _now()
        grant.version += 1
    else:
        return grant
    await session.flush()
    await _record_grant_event(session, "execution.granted", "Execution granted", grant, granted_by)
    return grant


async def revoke_grant(session: AsyncSession, project_id: uuid.UUID, principal_id: int,
                       revoked_by: str) -> TeamExecutionGrant:
    from brain.team.provisioning import TeamNotFound

    require_team_storage()
    grant = await fresh(session, TeamExecutionGrant, TeamExecutionGrant.project_id == project_id,
                        TeamExecutionGrant.principal_id == principal_id, exclusive=True)
    if grant is None:
        raise TeamNotFound("Execution grant not found")
    if grant.revoked_at is None:
        grant.revoked_at, grant.revoked_by = _now(), revoked_by[:128]
        grant.version += 1
        await session.flush()
        await _record_grant_event(session, "execution.revoked", "Execution grant revoked", grant, revoked_by)
    return grant


_PRINCIPAL_ACTOR = re.compile(r"^principal:(\d+)$")


async def _record_grant_event(session: AsyncSession, kind: str, summary: str, grant: TeamExecutionGrant,
                              by: str) -> None:
    """Team-administration activity for an effective grant change (``by`` is ``principal:<id>`` or an operator).

    Grants a membership or project-role revocation withdraws are not logged
    here; that revocation's own ``membership``/``project_role`` event covers it."""
    match = _PRINCIPAL_ACTOR.match(by)
    actor_id = int(match.group(1)) if match else None
    await record_event(session, kind=kind, summary=summary, audience="admin", actor_principal_id=actor_id,
                       actor_label=None if actor_id else "operator", subject_principal_id=grant.principal_id,
                       project_id=grant.project_id,
                       details={"principal_id": grant.principal_id, "version": grant.version})


async def list_grants(session: AsyncSession, project_id: uuid.UUID) -> list[dict]:
    rows = (await session.execute(select(TeamExecutionGrant).where(TeamExecutionGrant.project_id == project_id)
                                  .order_by(TeamExecutionGrant.principal_id))).scalars().all()
    return [grant_dict(row) for row in rows]


async def grant_execution(session: AsyncSession, caller: ResolvedPrincipal, project_id: uuid.UUID,
                          principal_id: int) -> dict:
    await administrator(session, caller, project_id)
    return grant_dict(await set_grant(session, project_id, principal_id, f"principal:{caller.id}"))


async def revoke_execution(session: AsyncSession, caller: ResolvedPrincipal, project_id: uuid.UUID,
                           principal_id: int) -> dict:
    await administrator(session, caller, project_id)
    return grant_dict(await revoke_grant(session, project_id, principal_id, f"principal:{caller.id}"))


async def list_execution_grants(session: AsyncSession, caller: ResolvedPrincipal, project_id: uuid.UUID) -> list[dict]:
    await administrator(session, caller, project_id)
    return await list_grants(session, project_id)


async def list_llm_consents(session: AsyncSession, caller: ResolvedPrincipal, project_id: uuid.UUID) -> list[dict]:
    await authorize(session, caller, project_id, Permission.READ)
    return await execution_llm.list_consents(session, project_id)


async def consent_llm(session: AsyncSession, caller: ResolvedPrincipal, project_id: uuid.UUID, provider: str,
                      model: str = execution_llm.ANY_MODEL) -> dict:
    await administrator(session, caller, project_id)
    row = await execution_llm.set_consent(session, project_id, provider, model, f"principal:{caller.id}",
                                          providers=operator_policy().llm_providers)
    return execution_llm.consent_dict(row)


async def revoke_llm_consent(session: AsyncSession, caller: ResolvedPrincipal, project_id: uuid.UUID, provider: str,
                             model: str = execution_llm.ANY_MODEL) -> dict:
    await administrator(session, caller, project_id)
    row = await execution_llm.revoke_consent(session, project_id, provider, model, f"principal:{caller.id}")
    return execution_llm.consent_dict(row)


async def llm_budget(session: AsyncSession, caller: ResolvedPrincipal, project_id: uuid.UUID) -> dict:
    await authorize(session, caller, project_id, Permission.READ)
    return await execution_llm.budget_report(session, project_id)


async def set_llm_budget(session: AsyncSession, caller: ResolvedPrincipal, project_id: uuid.UUID,
                         limits: Mapping[str, Any]) -> dict:
    await administrator(session, caller, project_id)
    await execution_llm.set_budget(session, project_id, f"principal:{caller.id}", **limits)
    return await execution_llm.budget_report(session, project_id)


async def clear_llm_budget(session: AsyncSession, caller: ResolvedPrincipal, project_id: uuid.UUID) -> dict:
    await administrator(session, caller, project_id)
    await execution_llm.clear_budget(session, project_id, f"principal:{caller.id}")
    return await execution_llm.budget_report(session, project_id)
