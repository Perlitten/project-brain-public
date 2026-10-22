"""REST API Router for Workspace and Repository Registry.

Phase A6 — Server endpoints for repository registration,
inspection, and capability management.
"""

import os
import uuid
from pathlib import Path
from typing import Annotated, List

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, StringConstraints

from apps.api import team_access
from apps.api.auth import require_api_key, require_principal, require_scope
from apps.api.registry_access import (
    disable_registered,
    enable_registered,
    read_registered,
    register_for_team,
    registry_authority,
    workspace_events,
    workspace_registry,
)
from apps.api.team_access import team_administrator_access
from brain.auth.principals import ResolvedPrincipal
from brain.lab.laboratory import BUILTIN_PROFILES
from brain.team.authorization import Permission
from brain.workspace.capabilities import CapabilitiesManager
from brain.workspace.models import (
    TrustLevel,
    RepositoryRecord,
    _generate_repository_id,
)
from brain.workspace.path_sandbox import PathSandbox, PathValidationError
from brain.workspace.registry_store import RegistryStore


router = APIRouter(
    prefix="/workspace",
    tags=["workspace-registry"],
    dependencies=[Depends(require_api_key), Depends(require_scope("workspace:read"))],
)


def _get_store() -> RegistryStore:
    """The solo-state file store; reads go through ``workspace_registry`` (the shared table in team state)."""
    return RegistryStore(Path(".brain/workspace"))


class RegisterRequest(BaseModel):
    display_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
    path: str
    trust_level: str = "trusted_internal"
    organization: str = ""
    owner: str = ""
    allowed_roots: List[str] = []
    # Team state only: the team project whose members may use the repository. Ignored in solo state.
    project_id: uuid.UUID | None = None
    # Explicitly provision an empty index identity when adding a new team source.
    create_index_record: bool = False


class UpdateCapabilitiesRequest(BaseModel):
    grant: List[str] = []
    revoke: List[str] = []
    expected_version: int


# The registry listing spans every repository: a team administrator once members exist
# (filtering it by membership is a follow-up, like the indexed-repository listing).
@router.get("/repositories", dependencies=[Depends(require_scope("workspace:read")), Depends(team_administrator_access())])
async def list_repositories():
    """List all registered repositories."""
    repos = (await workspace_registry()).list_all()
    return {
        "status": "success",
        "total": len(repos),
        "repositories": [r.to_dict() for r in repos],
    }


async def _require_registration_write(principal: ResolvedPrincipal = Depends(require_principal)) -> None:
    # Existing team-administrator credentials intentionally carry workspace:read,
    # not the solo registry's workspace:write scope. Team membership and the
    # project-level administrator check below authorize this team-only operation;
    # solo callers keep the original explicit write-scope requirement.
    if await team_access.current_team_state() == "solo" and not principal.has_scope("workspace:write"):
        raise HTTPException(status_code=403, detail="Missing required scope: workspace:write")


@router.post("/repositories", dependencies=[Depends(team_administrator_access()), Depends(_require_registration_write)])
async def register_repository(req: RegisterRequest, principal: ResolvedPrincipal = Depends(require_principal)):
    """Register a new repository.

    Trust level is resolved server-side — do not accept trusted roles from request body.
    In team state the repository is also linked to a team project the caller administers
    (``register_for_team``); a registration no member could use is refused.
    """
    # Validate trust level
    try:
        trust = TrustLevel(req.trust_level)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid trust level: {req.trust_level}")

    # Validate path
    existing = (await workspace_registry()).list_all()
    registered_canonicals = {
        PathSandbox.get_canonical_string(Path(r.canonical_root))
        for r in existing
        if not r.disabled
    }

    # Roots are canonicalised with os.path.realpath (a pure path operation); the
    # requested path's parent is an allowed root by design for this admin-scoped
    # endpoint, so the sandbox enforces shape and git layout, not location.
    allowed_roots = [Path(os.path.dirname(os.path.realpath(req.path))), Path.cwd().resolve()]
    for r in req.allowed_roots:
        allowed_roots.append(Path(os.path.realpath(r)))

    sandbox = PathSandbox(allowed_roots, registered_canonicals=registered_canonicals)
    require_git = trust != TrustLevel.FIXTURE

    try:
        resolved = sandbox.validate(req.path, require_git=require_git)
    except PathValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    canonical = str(resolved)
    normalized = PathSandbox.get_normalized_identity(resolved)
    if len(canonical) > 1024 or len(normalized) > 1024:
        raise HTTPException(status_code=422, detail="Canonical repository path exceeds the 1024-character storage limit")
    repo_id = _generate_repository_id(canonical)

    revision = PathSandbox.get_git_revision(resolved) if require_git else "none"
    default_branch = PathSandbox.get_git_default_branch(resolved) if require_git else "main"
    languages = PathSandbox.detect_languages(resolved)
    capabilities = CapabilitiesManager.get_defaults(trust)

    record = RepositoryRecord(
        repository_id=repo_id,
        display_name=req.display_name,
        canonical_root=canonical,
        normalized_root_identity=normalized,
        repository_type=("fixture" if trust == TrustLevel.FIXTURE else "git"),
        default_branch=default_branch,
        current_revision=revision,
        trust_level=trust.value,
        organization=req.organization,
        owner=req.owner,
        languages=languages,
        capabilities=capabilities,
        # Both built-in profiles, not just one: the laboratory refuses any
        # profile outside this list, and the CLIs default to "python-compile".
        # Allowing a single profile here made the default validate call fail.
        allowed_validation_profiles=sorted(BUILTIN_PROFILES),
    )

    try:
        saved, project_id = await register_for_team(
            principal, canonical, req.project_id, record,
            lambda: _get_store().register(record, actor="api", source="api"),
            create_index_record=req.create_index_record)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    response = {"status": "success", "repository": saved.to_dict()}
    if project_id is not None:
        response["project_id"] = str(project_id)
    return response


@router.get("/repositories/{repository_id}", dependencies=[Depends(require_scope("workspace:read"))])
async def get_repository(repository_id: str, principal: ResolvedPrincipal = Depends(require_principal)):
    """Get repository details.

    A disabled repository is readable by a team administrator in team state (audited as
    ``registry.inspected``), so it can be inspected before it is re-enabled."""
    record = (await workspace_registry()).get(repository_id)
    await read_registered(principal, record)
    if not record:
        raise HTTPException(status_code=404, detail=f"Repository '{repository_id}' not found")
    return {"status": "success", "repository": record.to_dict()}


# Trust and lifecycle of a registered repository are registry administration, not a project role.
@router.post("/repositories/{repository_id}/disable",
             dependencies=[Depends(require_scope("workspace:write")), Depends(team_administrator_access())])
async def disable_repository(repository_id: str, reason: str = Query("manual disable"),
                             principal: ResolvedPrincipal = Depends(require_principal)):
    """Disable a repository."""
    try:
        record = await disable_registered(principal, repository_id, reason)
        return {"status": "success", "repository": record.to_dict()}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/repositories/{repository_id}/enable",
             dependencies=[Depends(require_scope("workspace:write")), Depends(team_administrator_access())])
async def enable_repository(repository_id: str,
                            trust_level: str | None = Query(None, description="Defaults to the level before disable"),
                            reason: str = Query("manual enable"),
                            principal: ResolvedPrincipal = Depends(require_principal)):
    """Re-enable a disabled repository, at ``trust_level`` or at the trust level it had before it was disabled."""
    if trust_level is not None:
        try:
            TrustLevel(trust_level)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid trust level: {trust_level}")
    try:
        record = await enable_registered(principal, repository_id, trust_level, reason)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"status": "success", "repository": record.to_dict()}


@router.get("/repositories/{repository_id}/capabilities", dependencies=[Depends(require_scope("workspace:read"))])
async def get_capabilities(repository_id: str, principal: ResolvedPrincipal = Depends(require_principal)):
    """Get repository capabilities with validation."""
    record = (await workspace_registry()).get(repository_id)
    await registry_authority(principal, Permission.READ, record)
    if not record:
        raise HTTPException(status_code=404, detail=f"Repository '{repository_id}' not found")

    trust = TrustLevel(record.trust_level)
    issues = CapabilitiesManager.validate_capabilities(record.capabilities, trust)

    return {
        "status": "success",
        "repository_id": repository_id,
        "trust_level": record.trust_level,
        "capabilities": record.capabilities,
        "validation_issues": issues,
    }


@router.get("/repositories/{repository_id}/events", dependencies=[Depends(require_scope("workspace:read"))])
async def get_repository_events(repository_id: str, limit: int = Query(50, le=500),
                                principal: ResolvedPrincipal = Depends(require_principal)):
    """Get audit events for a repository."""
    await registry_authority(principal, Permission.READ, (await workspace_registry()).get(repository_id))
    events = await workspace_events(repository_id, limit)
    return {
        "status": "success",
        "total": len(events),
        "events": [e.to_dict() for e in events],
    }


@router.get("/verify", dependencies=[Depends(require_scope("workspace:read")), Depends(team_administrator_access())])
async def verify_registry():
    """Verify registry integrity."""
    ok, msg = (await workspace_registry()).verify_integrity()
    return {
        "status": "success" if ok else "error",
        "integrity": ok,
        "message": msg,
    }
