"""Repository authority for the personal API surfaces (unified team model).

A Brain is a team of one until the first member joins: in that solo state
every authenticated caller is the implicit administrator of every indexed
repository, so nothing here touches the team tables beyond the state probe.
Once a live team member exists, repository access is decided by the project
roles in ``brain.team.authorization``; an unscoped or unattached call needs a
team administrator, and the shared legacy key or dev-open access is refused.

Handlers that read their scope from the request body call
``repository_authority`` after validation; routers whose scope is a query
parameter declare ``Depends(repository_access(permission))``.
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from apps.api.auth import require_principal
from brain.auth.principals import ResolvedPrincipal
from brain.database.repository_utils import find_repository_id
from brain.database.session import async_session_factory
from brain.team.authorization import (
    Authority,
    Permission,
    TeamDenied,
    TeamState,
    authorize_repository,
    solo_authority,
    team_state,
)


async def current_team_state() -> TeamState:
    """``"solo"`` or ``"team"`` for this Brain right now (one short query in team-capable storage)."""
    async with async_session_factory() as session:
        return await team_state(session)


async def repository_authority(principal: ResolvedPrincipal, permission: Permission, *,
                               repository_id: int | None = None, repo_path: str | None = None) -> Authority:
    """Authority of ``principal`` over one repository, given by id or by indexed path.

    Neither argument means the unscoped, every-repository surface. A path that
    is not indexed is treated as unattached. Denials become HTTP 403 so every
    personal route reports them the same way."""
    if await current_team_state() == "solo":
        return solo_authority(principal)
    try:
        async with async_session_factory() as session:
            if repository_id is None and repo_path:
                repository_id = await find_repository_id(session, repo_path)
            return await authorize_repository(session, principal, repository_id, permission)
    except TeamDenied as exc:
        raise HTTPException(status_code=403, detail=f"Repository access denied: {exc}") from exc


def _query_scope(request: Request) -> tuple[int | None, str | None]:
    """``repository_id`` / ``repo_path`` as the query string spells them; malformed ids count as unscoped."""
    params = request.query_params
    raw_id = params.get("repository_id")
    repository_id = int(raw_id) if raw_id is not None and raw_id.isdigit() else None
    repo_path = params.get("repo_path") or None
    return repository_id, repo_path


def repository_access(permission: Permission):
    """Dependency: authorize ``permission`` on the repository named by the query string."""

    async def dependency(request: Request, principal: ResolvedPrincipal = Depends(require_principal)) -> Authority:
        repository_id, repo_path = _query_scope(request)
        return await repository_authority(principal, permission, repository_id=repository_id, repo_path=repo_path)

    return dependency
