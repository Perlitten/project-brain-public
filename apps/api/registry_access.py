"""Team-state authorization for the workspace-registry routers.

Freshness, ledger, workspace, operations, lab and experiments name repositories
by workspace-registry ids (``repo-<hash>``), not by ``repositories.id``. The
registry record's canonical root is the indexed path ``repository_authority``
resolves to a project, so a registry id that is unknown, or whose root is not
indexed, counts as an unattached repository: a team administrator in team
state. Solo state is unchanged (``repository_authority`` short-circuits).

Kept beside ``apps/api/team_access.py`` rather than in it so that module keeps a
single owner; both helpers only compose ``repository_authority``.
"""
from __future__ import annotations

from fastapi import Depends

from apps.api.auth import require_principal
from apps.api.team_access import repository_authority
from brain.auth.principals import ResolvedPrincipal
from brain.team.authorization import Authority, Permission
from brain.workspace.models import RepositoryRecord


async def team_admin_access(principal: ResolvedPrincipal = Depends(require_principal)) -> Authority:
    """Dependency for global and cross-repository surfaces: a team administrator once members exist.

    Unlike ``repository_access`` it ignores ``repository_id``/``repo_path`` in the
    query string, so naming one's own repository cannot open a global route."""
    return await repository_authority(principal, Permission.READ)


async def registry_authority(principal: ResolvedPrincipal, permission: Permission,
                             record: RepositoryRecord | None) -> Authority:
    """Authority over the repository a workspace-registry record points at; ``None`` is unattached."""
    return await repository_authority(principal, permission,
                                      repo_path=record.canonical_root if record is not None else None)
