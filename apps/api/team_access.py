"""Repository authority for the personal API surfaces (unified team model).

A Brain is a team of one until the first member joins: in that solo state
every authenticated caller is the implicit administrator of every indexed
repository, so nothing here touches the team tables beyond the state probe.
Once a live team member exists, repository access is decided by the project
roles in ``brain.team.authorization``; an unscoped or unattached call needs a
team administrator, and the shared legacy key or dev-open access is refused.

Handlers that read their scope from the request body call
``repository_authority`` after validation; routers whose scope is a query
parameter declare ``Depends(repository_access(permission))``. Global and
administrative routes that belong to no repository declare
``Depends(team_administrator_access())``; status routes that members may
still read call ``is_team_administrator`` to trim what they return, and
listings call ``readable_repository_ids`` / ``readable_repository_scopes``
to keep the rows of repositories the caller may read (query-scoped listings
mark themselves ``filters_unreadable`` so members may call them unscoped).
"""
from __future__ import annotations

import uuid
from typing import Protocol, TypeVar

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from apps.api import auth
from apps.api.auth import require_principal
from brain.auth.principals import ResolvedPrincipal
from brain.database.models import Repository
from brain.database.repository_utils import find_repository_id
from brain.database.session import async_session_factory
from brain.memory.repo_scope import normalize_repo_scope
from brain.team.authorization import (
    ROLE_PERMISSIONS,
    Authority,
    Permission,
    TeamDenied,
    TeamState,
    authorize,
    authorize_repository,
    human_member,
    live_credential,
    permissions,
    registry_records,
    registry_trust_ceiling,
    solo_authority,
    team_administrator,
    team_state,
)
from brain.team.models import AgentRun, ProjectMember, ProjectRepository, TeamProject, TeamTaskBinding
from brain.team.registry import ensure_legacy_imported
from brain.team import write_fence


class MemoryScopedRow(Protocol):
    repo_path: str | None


MemoryRow = TypeVar("MemoryRow", bound=MemoryScopedRow)


async def current_team_state() -> TeamState:
    """``"solo"`` or ``"team"`` for this Brain right now (one short query in team-capable storage)."""
    async with async_session_factory() as session:
        return await team_state(session)


async def repository_authority(principal: ResolvedPrincipal, permission: Permission, *,
                               repository_id: int | None = None, repo_path: str | None = None,
                               author_id: int | None = None) -> Authority:
    """Authority of ``principal`` over one repository, given by id or by indexed path.

    Neither argument means the unscoped, every-repository surface. A path that
    is not indexed is treated as unattached. The workspace registry trust of
    the path applies as well, indexed or not (``authorize_repository``).
    Denials become HTTP 403 so every personal route reports them the same way."""
    if await current_team_state() == "solo":
        return solo_authority(principal)
    # The first team-state decision moves this process's legacy registry file into the shared table.
    await ensure_legacy_imported(async_session_factory)
    try:
        async with async_session_factory() as session:
            if repository_id is None and repo_path:
                repository_id = await find_repository_id(session, repo_path)
            authority = await authorize_repository(session, principal, repository_id, permission,
                                                   author_id=author_id, repo_path=repo_path)
    except TeamDenied as exc:
        raise HTTPException(status_code=403, detail=f"Repository access denied: {exc}") from exc
    _arm(principal, authority)
    return authority


async def team_denied_response(request: Request, exc: Exception) -> JSONResponse:
    """A team refusal raised below the route (the write fence after a revocation) is HTTP 403, never a 500."""
    return JSONResponse(status_code=403, content={"detail": f"Team access denied: {exc}"})


def _arm(principal: ResolvedPrincipal, authority: Authority) -> None:
    """Fence the rest of this request's writes on the identities just authorized (``brain/team/write_fence.py``)."""
    if authority.team_state == "team":
        write_fence.arm(authority.actor_id, principal.credential_id, authority.initiator_id,
                        authority.initiator_credential_id)


_SCOPE_KEYS = frozenset({"repository_id", "repo_path"})


def _query_scope(request: Request) -> tuple[int | None, str | None]:
    """``repository_id`` / ``repo_path`` as the query string spells them; malformed ids count as unscoped.

    When the matched route declares one of the two keys, only the keys it declares count: a key the
    handler ignores would otherwise authorize one repository while the handler falls back to another
    (its default repository, or every repository). Routes that declare neither check their object's
    repository themselves and keep accepting either key."""
    params = request.query_params
    dependant = getattr(request.scope.get("route"), "dependant", None)
    declared = {field.alias for field in dependant.query_params} & _SCOPE_KEYS if dependant else set()
    keys = declared or _SCOPE_KEYS
    raw_id = params.get("repository_id") if "repository_id" in keys else None
    repository_id = int(raw_id) if raw_id is not None and raw_id.isdigit() else None
    repo_path = (params.get("repo_path") or None) if "repo_path" in keys else None
    return repository_id, repo_path


def filters_unreadable(endpoint):
    """Mark a listing that, called without a repository, keeps only the rows the caller may read.

    ``repository_access`` then lets the unscoped call through instead of requiring a team
    administrator; the handler filters with ``readable_repository_ids`` / ``readable_repository_scopes``."""
    endpoint.filters_unreadable = True
    return endpoint


def repository_access(permission: Permission):
    """Dependency: authorize ``permission`` on the repository named by the query string."""

    async def dependency(request: Request, principal: ResolvedPrincipal = Depends(require_principal)) -> Authority | None:
        repository_id, repo_path = _query_scope(request)
        endpoint = getattr(request.scope.get("route"), "endpoint", None)
        if repository_id is None and repo_path is None and getattr(endpoint, "filters_unreadable", False):
            return None
        return await repository_authority(principal, permission, repository_id=repository_id, repo_path=repo_path)

    return dependency


async def readable_repository_ids(principal: ResolvedPrincipal, repository_ids: list[int]) -> set[int] | None:
    """Ids among ``repository_ids`` the caller may ``read``; ``None`` means every repository.

    Solo state and team administrators pass the unscoped check and see all; any
    other team member sees the repositories their project roles grant, minus
    those the workspace registry no longer trusts for reading."""
    if await current_team_state() == "solo":
        return None

    async with async_session_factory() as session:
        try:
            live = await live_credential(session, principal.id, principal.credential_id)
        except TeamDenied:
            return set()

        # A run is bound to exactly one task repository. One normal authority
        # check preserves the delegation-chain and task-binding rules without
        # multiplying it by the installation's repository count.
        if live.kind == "run":
            repository_id = (await session.execute(
                select(TeamTaskBinding.repository_id)
                .join(AgentRun, AgentRun.task_id == TeamTaskBinding.task_id)
                .where(AgentRun.principal_id == live.id, AgentRun.credential_id == live.credential_id)
            )).scalar_one_or_none()
            if repository_id is None or repository_id not in repository_ids:
                return set()
            try:
                await authorize_repository(session, principal, repository_id, Permission.READ)
            except TeamDenied:
                return set()
            return {repository_id}

        if live.kind != "human" or not live.has_scope("team:read"):
            return set()
        try:
            member = await human_member(session, live, 1)
        except TeamDenied:
            return set()
        if member.is_admin and live.has_scope("team:admin"):
            return None
        if not repository_ids:
            return set()

        # Fetch every candidate's project policy and this principal's role in
        # one joined query, then apply the same role/policy intersection as
        # ``authorize``. This avoids a complete authorization query sequence
        # for every repository row.
        rows = (await session.execute(
            select(ProjectRepository.repository_id, ProjectMember.role, TeamProject.permissions)
            .join(TeamProject, TeamProject.id == ProjectRepository.project_id)
            .join(
                ProjectMember,
                (ProjectMember.project_id == ProjectRepository.project_id)
                & (ProjectMember.principal_id == live.id),
            )
            .where(
                ProjectRepository.repository_id.in_(repository_ids),
                ProjectMember.revoked_at.is_(None),
                TeamProject.disabled_at.is_(None),
            )
        )).all()
        granted = {
            repository_id
            for repository_id, role, policy in rows
            if Permission.READ in ROLE_PERMISSIONS.get(role, frozenset())
            and Permission.READ in _policy_permissions(policy)
        }
        if not granted:
            return granted
        # Registry trust caps membership here exactly as in ``authorize_repository``.
        try:
            records = await registry_records(session)
        except TeamDenied:
            return set()
        if not records:
            return granted
        paths = (await session.execute(
            select(Repository.id, Repository.path).where(Repository.id.in_(granted)))).all()
        return {
            repository_id for repository_id, path in paths
            if Permission.READ in registry_trust_ceiling(records, path)[0]
        }


async def readable_project_ids(principal: ResolvedPrincipal) -> set[uuid.UUID] | None:
    """Projects the caller may ``read``; ``None`` means every project.

    Solo state and team administrators see all; a human member sees the enabled
    projects whose role and policy both grant ``read``; anything else sees none."""
    if await current_team_state() == "solo":
        return None
    async with async_session_factory() as session:
        try:
            live = await live_credential(session, principal.id, principal.credential_id)
            if live.kind != "human" or not live.has_scope("team:read"):
                return set()
            member = await human_member(session, live, 1)
        except TeamDenied:
            return set()
        if member.is_admin and live.has_scope("team:admin"):
            return None
        rows = (await session.execute(
            select(ProjectMember.project_id, ProjectMember.role, TeamProject.permissions)
            .join(TeamProject, TeamProject.id == ProjectMember.project_id)
            .where(ProjectMember.principal_id == live.id, ProjectMember.revoked_at.is_(None),
                   TeamProject.disabled_at.is_(None))
        )).all()
    return {
        project_id
        for project_id, role, policy in rows
        if Permission.READ in ROLE_PERMISSIONS.get(role, frozenset())
        and Permission.READ in _policy_permissions(policy)
    }


def _policy_permissions(policy: list[str] | None) -> frozenset[Permission]:
    """A project's granted permissions; an invalid policy grants nothing, as ``authorize`` denies it."""
    try:
        return permissions(policy or [])
    except TeamDenied:
        return frozenset()


async def readable_repository_scopes(principal: ResolvedPrincipal) -> set[str] | None:
    """Normalized paths of the indexed repositories the caller may ``read``; ``None`` means every one.

    For memory keyed by ``repo_path`` (decisions, rules): global rows stay visible to every
    member, repository rows only where ``readable_repository_ids`` grants the repository."""
    if await current_team_state() == "solo":
        return None
    async with async_session_factory() as session:
        repositories = (await session.execute(select(Repository.id, Repository.path))).all()
    readable = await readable_repository_ids(principal, [repository_id for repository_id, _ in repositories])
    if readable is None:
        return None
    return {
        scope
        for repository_id, path in repositories
        if repository_id in readable and (scope := normalize_repo_scope(path)) is not None
    }


async def readable_memory_rows(principal: ResolvedPrincipal, rows: list[MemoryRow]) -> list[MemoryRow]:
    """Global memory plus repository rows readable by a live human team member.

    Memory keeps the path spelling submitted by the writer, while repository
    authorization accepts relative, slash-normalized and real-path aliases.
    Resolve each distinct stored spelling through that same lookup before
    applying the batched repository permissions.
    """
    if await current_team_state() == "solo":
        return rows

    async with async_session_factory() as session:
        try:
            live = await live_credential(session, principal.id, principal.credential_id)
            if live.kind != "human" or not live.has_scope("team:read"):
                raise TeamDenied("Verified team membership is required")
            member = await human_member(session, live, 1)
        except TeamDenied as exc:
            raise HTTPException(status_code=403, detail=f"Team member required: {exc}") from exc

        if member.is_admin and live.has_scope("team:admin"):
            return rows

        repository_by_scope: dict[str, int | None] = {}
        for row in rows:
            scope = normalize_repo_scope(row.repo_path)
            if scope is not None and scope not in repository_by_scope:
                repository_by_scope[scope] = await find_repository_id(session, row.repo_path or scope)

    candidate_ids = sorted({repository_id for repository_id in repository_by_scope.values()
                            if repository_id is not None})
    readable = await readable_repository_ids(principal, candidate_ids)
    if readable is None:
        return rows
    return [
        row for row in rows
        if (scope := normalize_repo_scope(row.repo_path)) is None or repository_by_scope.get(scope) in readable
    ]


async def member_authority(principal: ResolvedPrincipal) -> Authority:
    """Any live, verified human team member (read-only authority); solo state is the implicit administrator."""
    if await current_team_state() == "solo":
        return solo_authority(principal)
    try:
        async with async_session_factory() as session:
            live = await live_credential(session, principal.id, principal.credential_id)
            await human_member(session, live, 1)
    except TeamDenied as exc:
        raise HTTPException(status_code=403, detail=f"Team member required: {exc}") from exc
    assert live.credential_id is not None
    authority = Authority(live.id, live.id, live.credential_id, frozenset({Permission.READ}))
    _arm(principal, authority)
    return authority


async def proposer_authority(principal: ResolvedPrincipal) -> Authority:
    """Authority to propose system-global memory: ``knowledge.propose`` on at least one live project.

    Solo state is the implicit administrator. In team state a run never qualifies
    (runs cannot propose), and neither does a member whose roles are all Reader.
    Only team administrators approve what is proposed this way."""
    if await current_team_state() == "solo":
        return solo_authority(principal)
    denied: TeamDenied = TeamDenied("Permission denied")
    try:
        async with async_session_factory() as session:
            live = await live_credential(session, principal.id, principal.credential_id)
            project_ids = (await session.execute(
                select(ProjectMember.project_id)
                .join(TeamProject, TeamProject.id == ProjectMember.project_id)
                .where(ProjectMember.principal_id == live.id, ProjectMember.revoked_at.is_(None),
                       TeamProject.disabled_at.is_(None))
                .order_by(ProjectMember.project_id)
            )).scalars().all()
            for project_id in project_ids:
                try:
                    authority = await authorize(session, live, project_id, Permission.PROPOSE)
                except TeamDenied as exc:
                    denied = exc
                    continue
                _arm(principal, authority)
                return authority
    except TeamDenied as exc:
        denied = exc
    raise HTTPException(status_code=403, detail=f"Knowledge proposal denied: {denied}")


async def is_team_administrator(principal: ResolvedPrincipal) -> bool:
    """True in solo state or for a live team administrator; never raises, for routes that trim output."""
    if await current_team_state() == "solo":
        return True
    try:
        async with async_session_factory() as session:
            await team_administrator(session, principal)
    except TeamDenied:
        return False
    return True


def team_administrator_access():
    """Dependency: the global, every-repository surfaces need a team administrator once members exist.

    Solo state lets every authenticated caller through, as before. In team state
    ``require_principal`` already refuses the shared key and dev-open access
    (``Named credential required``); any other caller that is not a live team
    administrator holding ``team:admin`` gets HTTP 403."""

    async def dependency(request: Request) -> ResolvedPrincipal:
        # Reuse the principal the router-level auth dependency already resolved, as ``require_scope`` does.
        principal: ResolvedPrincipal | None = getattr(request.state, "principal", None)
        if principal is None:
            principal = await auth.require_principal(request, request.headers.get("x-api-key"))
        if await current_team_state() == "solo":
            return principal
        try:
            async with async_session_factory() as session:
                await team_administrator(session, principal)
        except TeamDenied as exc:
            raise HTTPException(status_code=403, detail=f"Team administrator required: {exc}") from exc
        write_fence.arm(principal.id, principal.credential_id, principal.id, principal.credential_id)
        return principal

    return dependency
