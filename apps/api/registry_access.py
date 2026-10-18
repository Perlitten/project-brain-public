"""Team-state authorization for the workspace-registry routers.

Freshness, ledger, workspace, operations, lab and experiments name repositories
by workspace-registry ids (``repo-<hash>``), not by ``repositories.id``. The
registry record's canonical root is the indexed path ``repository_authority``
resolves to a project, so a registry id that is unknown, or whose root is not
indexed, counts as an unattached repository: a team administrator in team
state. The record's own trust applies too (``authorize_repository``): a
disabled record is denied to everyone. Solo state is unchanged
(``repository_authority`` short-circuits). Global routes of these routers use
``team_access.team_administrator_access``.

Registering a repository in team state goes through ``register_for_team``: the
registry holds trust, team projects hold membership, and a registration that
links to no project would be trusted by the registry yet usable by no member.
"""
from __future__ import annotations

import uuid
from typing import Callable

from fastapi import HTTPException
from sqlalchemy import select

from apps.api import team_access
from apps.api.team_access import repository_authority
from brain.auth.principals import ResolvedPrincipal
from brain.database.models import Repository
from brain.database.repository_utils import find_repository_id
from brain.team import provisioning
from brain.team.authorization import Authority, Permission, TeamConflict, TeamDenied, administrator, fresh
from brain.team.models import ProjectRepository, TeamProject
from brain.workspace.models import RepositoryRecord


async def registry_authority(principal: ResolvedPrincipal, permission: Permission,
                             record: RepositoryRecord | None) -> Authority:
    """Authority over the repository a workspace-registry record points at; ``None`` is unattached."""
    return await repository_authority(principal, permission,
                                      repo_path=record.canonical_root if record is not None else None)


async def register_for_team(principal: ResolvedPrincipal, canonical_root: str, project_id: uuid.UUID | None,
                            register: Callable[[], RepositoryRecord]) -> tuple[RepositoryRecord, uuid.UUID | None]:
    """Register a repository in the workspace registry; in team state, also link it to a team project.

    Solo state calls ``register`` and returns no project. In team state the
    repository must already be indexed; it is attached to ``project_id`` (or
    must already be attached to a project when ``project_id`` is omitted), the
    caller must administer that project, and ``register`` runs inside the same
    database transaction, so a registry refusal rolls the link back. Raises
    ``HTTPException`` with a clear reason for every refusal; ``ValueError``
    from ``register`` propagates for the caller to map."""
    if await team_access.current_team_state() == "solo":
        return register(), None
    try:
        async with team_access.async_session_factory() as session, session.begin():
            repository_id = await find_repository_id(session, canonical_root)
            if repository_id is None:
                raise HTTPException(status_code=409, detail=(
                    "In team state, a repository must be indexed before it is registered, so it can be "
                    "linked to a team project: index it, then register it with project_id"))
            if project_id is None:
                link = await fresh(session, ProjectRepository, ProjectRepository.repository_id == repository_id)
                if link is None:
                    raise HTTPException(status_code=422, detail=(
                        "In team state, registering a repository needs project_id: the team project "
                        "whose members may use it"))
                project_id = link.project_id
                await administrator(session, principal, project_id)
            else:
                project = await fresh(session, TeamProject, TeamProject.id == project_id)
                if project is None:
                    raise HTTPException(status_code=404, detail="Project not found")
                if project.disabled_at:
                    raise HTTPException(status_code=409, detail="Project is disabled")
                await administrator(session, principal, project_id)
                repository = (await session.execute(
                    select(Repository).where(Repository.id == repository_id))).scalar_one()
                await provisioning.attach_repository(session, project_id=project_id, repository=repository)
            return register(), project_id
    except TeamDenied as exc:
        raise HTTPException(status_code=403, detail=f"Team administrator required: {exc}") from exc
    except provisioning.TeamNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TeamConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
