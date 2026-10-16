"""Team-state authorization for the workspace-registry routers.

Freshness, ledger, workspace, operations, lab and experiments name repositories
by workspace-registry ids (``repo-<hash>``), not by ``repositories.id``. The
registry record's canonical root is the indexed path ``repository_authority``
resolves to a project, so a registry id that is unknown, or whose root is not
indexed, counts as an unattached repository: a team administrator in team
state. Solo state is unchanged (``repository_authority`` short-circuits).
Global routes of these routers use ``team_access.team_administrator_access``.
"""
from __future__ import annotations

from apps.api.team_access import repository_authority
from brain.auth.principals import ResolvedPrincipal
from brain.team.authorization import Authority, Permission
from brain.workspace.models import RepositoryRecord


async def registry_authority(principal: ResolvedPrincipal, permission: Permission,
                             record: RepositoryRecord | None) -> Authority:
    """Authority over the repository a workspace-registry record points at; ``None`` is unattached."""
    return await repository_authority(principal, permission,
                                      repo_path=record.canonical_root if record is not None else None)
