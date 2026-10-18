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

Which registry: solo state reads and writes the file store
(``.brain/workspace``), as before. Team state reads the shared
``workspace_registry_entries`` table (``brain.team.registry``) that the worker
and the CLI read too: ``workspace_registry`` returns the reader, and every
team-state change (register + project link, disable, re-enable) commits in one
database transaction with its ``registry.*`` row in the team activity feed.
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Awaitable, Callable

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api import team_access
from apps.api.team_access import repository_authority
from brain.auth.principals import ResolvedPrincipal
from brain.database.models import Repository
from brain.database.repository_utils import find_repository_id
from brain.team import provisioning
from brain.team import registry as shared
from brain.team.authorization import (Authority, Permission, TeamConflict, TeamDenied, administrator, fresh,
                                      live_credential, team_administrator)
from brain.team.models import ProjectRepository, TeamProject
from brain.workspace.models import RegistryEvent, RepositoryRecord
from brain.workspace.registry_store import OptimisticLockError, RegistryStore

REGISTRY_DIR = Path(".brain/workspace")


async def registry_authority(principal: ResolvedPrincipal, permission: Permission,
                             record: RepositoryRecord | None) -> Authority:
    """Authority over the repository a workspace-registry record points at; ``None`` is unattached."""
    return await repository_authority(principal, permission,
                                      repo_path=record.canonical_root if record is not None else None)


def _file_store() -> RegistryStore:
    return RegistryStore(REGISTRY_DIR)


async def workspace_registry() -> shared.RegistrySnapshot | RegistryStore:
    """The registry to read: the file store in solo state, the shared table in team state."""
    if await team_access.current_team_state() == "solo":
        return _file_store()
    await shared.ensure_legacy_imported(team_access.async_session_factory, REGISTRY_DIR)
    async with team_access.async_session_factory() as session:
        return await shared.team_registry(session, REGISTRY_DIR)


async def workspace_events(repository_id: str, limit: int) -> list[RegistryEvent]:
    """Registry audit events of one repository, from the registry ``workspace_registry`` reads."""
    if await team_access.current_team_state() == "solo":
        return _file_store().list_events(repository_id=repository_id, limit=limit)
    async with team_access.async_session_factory() as session:
        return await shared.team_events(session, repository_id, limit, REGISTRY_DIR)


async def import_registry_file_at_startup() -> None:
    """In team state, import this process's legacy registry file into the shared table (once, while it is empty).

    Every team-state read does the same on first use; running it at startup
    puts the import (and its log line) next to the deploy that triggers it."""
    if await team_access.current_team_state() != "solo":
        await shared.ensure_legacy_imported(team_access.async_session_factory, REGISTRY_DIR)


def _actor(principal: ResolvedPrincipal) -> str:
    return f"principal:{principal.id}" if principal.id and principal.id > 0 else "api"


async def register_for_team(principal: ResolvedPrincipal, canonical_root: str, project_id: uuid.UUID | None,
                            record: RepositoryRecord,
                            register: Callable[[], RepositoryRecord]) -> tuple[RepositoryRecord, uuid.UUID | None]:
    """Register a repository in the workspace registry; in team state, also link it to a team project.

    Solo state calls ``register`` (the file store) and returns no project. In
    team state the repository must already be indexed; it is attached to
    ``project_id`` (or must already be attached to a project when
    ``project_id`` is omitted), the caller must administer that project, and
    ``record`` is inserted into the shared registry table in the same database
    transaction as the link and its ``registry.registered`` activity row, so a
    refusal anywhere rolls all of them back. Raises ``HTTPException`` with a
    clear reason for every refusal; ``ValueError`` (already registered)
    propagates for the caller to map."""
    if await team_access.current_team_state() == "solo":
        return register(), None
    await shared.ensure_legacy_imported(team_access.async_session_factory, REGISTRY_DIR)
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
            saved = await shared.insert_entry(session, record, actor=_actor(principal), source="api")
            await shared.audit(session, "registered", saved,
                               f"Registered repository {saved.display_name} ({saved.trust_level})",
                               actor_principal_id=principal.id, project_id=project_id)
            return saved, project_id
    except TeamDenied as exc:
        raise HTTPException(status_code=403, detail=f"Team administrator required: {exc}") from exc
    except provisioning.TeamNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TeamConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


async def _team_change(principal: ResolvedPrincipal, kind: str,
                       change: Callable[[AsyncSession], Awaitable[RepositoryRecord]]) -> RepositoryRecord:
    """Run one registry ``change`` and its ``registry.<kind>`` activity row in one team-state transaction."""
    await shared.ensure_legacy_imported(team_access.async_session_factory, REGISTRY_DIR)
    try:
        async with team_access.async_session_factory() as session, session.begin():
            record = await change(session)
            await shared.audit(session, kind, record,
                               f"{kind.capitalize()} repository {record.display_name} ({record.trust_level})",
                               actor_principal_id=principal.id)
            return record
    except shared.RegistryConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


async def disable_registered(principal: ResolvedPrincipal, repository_id: str, reason: str) -> RepositoryRecord:
    """Withdraw a repository's trust; ``ValueError`` when the id is unknown."""
    if await team_access.current_team_state() == "solo":
        return _file_store().disable(repository_id, actor="api", source="api", reason=reason)
    return await _team_change(principal, "disabled", lambda session: shared.disable_entry(
        session, repository_id, actor=_actor(principal), source="api", reason=reason))


async def enable_registered(principal: ResolvedPrincipal, repository_id: str, trust_level: str | None,
                            reason: str) -> RepositoryRecord:
    """Re-enable a disabled repository at ``trust_level``, or at the level it had before it was disabled.

    ``ValueError`` when the id or the level is unknown; HTTP 409 when the entry
    is not disabled or no earlier level is recorded."""
    if await team_access.current_team_state() == "team":
        return await _team_change(principal, "enabled", lambda session: shared.enable_entry(
            session, repository_id, trust_level=trust_level, actor=_actor(principal), source="api", reason=reason))
    store = _file_store()
    record = store.get(repository_id)
    if record is None:
        raise ValueError(f"Repository '{repository_id}' not found")
    if not shared.is_disabled(record):
        raise HTTPException(status_code=409, detail=f"Repository '{repository_id}' is not disabled")
    try:
        level = shared.restored_trust_level(store.list_events(repository_id=repository_id, limit=1_000_000),
                                            trust_level)
        return store.update(repository_id, {"disabled": False, "trust_level": level}, record.version,
                            actor="api", source="api", reason=reason)
    except (shared.RegistryConflict, OptimisticLockError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


async def read_registered(principal: ResolvedPrincipal, record: RepositoryRecord | None) -> None:
    """Authorize reading one registry entry by id; a team administrator may also open a disabled one.

    A disabled entry is denied to everyone by trust (``registry_authority``),
    which would leave no way to inspect it before re-enabling it. In team state
    a live team administrator holding ``team:admin`` may read it anyway; each
    such read is recorded as ``registry.inspected`` in the team activity feed."""
    try:
        await registry_authority(principal, Permission.READ, record)
        return
    except HTTPException as exc:
        if exc.status_code != 403 or record is None or not shared.is_disabled(record):
            raise
        denial = exc
    if await team_access.current_team_state() == "solo":
        raise denial
    try:
        async with team_access.async_session_factory() as session, session.begin():
            live = await live_credential(session, principal.id, principal.credential_id)
            await team_administrator(session, live)
            await shared.audit(session, "inspected", record, f"Opened disabled repository {record.display_name}",
                               actor_principal_id=principal.id)
    except TeamDenied:
        raise denial from None
