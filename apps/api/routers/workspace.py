"""REST API Router for Workspace and Repository Registry.

Phase A6 — Server endpoints for repository registration,
inspection, and capability management.
"""

from pathlib import Path
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from apps.api.auth import require_api_key, require_scope
from brain.lab.laboratory import BUILTIN_PROFILES
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
    return RegistryStore(Path(".brain/workspace"))


class RegisterRequest(BaseModel):
    display_name: str
    path: str
    trust_level: str = "trusted_internal"
    organization: str = ""
    owner: str = ""
    allowed_roots: List[str] = []


class UpdateCapabilitiesRequest(BaseModel):
    grant: List[str] = []
    revoke: List[str] = []
    expected_version: int


@router.get("/repositories", dependencies=[Depends(require_scope("workspace:read"))])
async def list_repositories():
    """List all registered repositories."""
    store = _get_store()
    repos = store.list_all()
    return {
        "status": "success",
        "total": len(repos),
        "repositories": [r.to_dict() for r in repos],
    }


@router.post("/repositories", dependencies=[Depends(require_scope("workspace:write"))])
async def register_repository(req: RegisterRequest):
    """Register a new repository.

    Trust level is resolved server-side — do not accept trusted roles from request body.
    """
    store = _get_store()

    # Validate trust level
    try:
        trust = TrustLevel(req.trust_level)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid trust level: {req.trust_level}")

    # Validate path
    existing = store.list_all()
    registered_canonicals = {
        PathSandbox.get_canonical_string(Path(r.canonical_root))
        for r in existing
        if not r.disabled
    }

    allowed_roots = [Path(req.path).resolve().parent, Path.cwd().resolve()]
    for r in req.allowed_roots:
        allowed_roots.append(Path(r).resolve())

    sandbox = PathSandbox(allowed_roots, registered_canonicals=registered_canonicals)
    require_git = trust != TrustLevel.FIXTURE

    try:
        resolved = sandbox.validate(req.path, require_git=require_git)
    except PathValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    canonical = str(resolved)
    normalized = PathSandbox.get_normalized_identity(resolved)
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
        saved = store.register(record, actor="api", source="api")
        return {"status": "success", "repository": saved.to_dict()}
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.get("/repositories/{repository_id}", dependencies=[Depends(require_scope("workspace:read"))])
async def get_repository(repository_id: str):
    """Get repository details."""
    store = _get_store()
    record = store.get(repository_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"Repository '{repository_id}' not found")
    return {"status": "success", "repository": record.to_dict()}


@router.post("/repositories/{repository_id}/disable", dependencies=[Depends(require_scope("workspace:write"))])
async def disable_repository(repository_id: str, reason: str = Query("manual disable")):
    """Disable a repository."""
    store = _get_store()
    try:
        record = store.disable(repository_id, actor="api", source="api", reason=reason)
        return {"status": "success", "repository": record.to_dict()}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/repositories/{repository_id}/capabilities", dependencies=[Depends(require_scope("workspace:read"))])
async def get_capabilities(repository_id: str):
    """Get repository capabilities with validation."""
    store = _get_store()
    record = store.get(repository_id)
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
async def get_repository_events(repository_id: str, limit: int = Query(50, le=500)):
    """Get audit events for a repository."""
    store = _get_store()
    events = store.list_events(repository_id=repository_id, limit=limit)
    return {
        "status": "success",
        "total": len(events),
        "events": [e.to_dict() for e in events],
    }


@router.get("/verify", dependencies=[Depends(require_scope("workspace:read"))])
async def verify_registry():
    """Verify registry integrity."""
    store = _get_store()
    ok, msg = store.verify_integrity()
    return {
        "status": "success" if ok else "error",
        "integrity": ok,
        "message": msg,
    }
