"""REST API Router for the Change Laboratory — Phase D9.

Every endpoint resolves the repository root server-side from the registry and
refuses to act without the capability the action requires. Patches are applied
only inside disposable managed workspaces; Project Brain never modifies the
authoritative checkout, commits, pushes, merges, or deploys.
"""

from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from apps.api.auth import require_api_key, require_scope
from brain.lab.laboratory import ChangeLaboratory
from brain.lab.models import PatchRecord, PatchSource
from brain.workspace.models import Capability
from brain.workspace.registry_store import RegistryStore

router = APIRouter(
    prefix="/lab",
    tags=["change-laboratory"],
    dependencies=[Depends(require_api_key), Depends(require_scope("lab:read"))],
)

MAX_PATCH_BYTES = 500_000


def _laboratory() -> ChangeLaboratory:
    return ChangeLaboratory(Path(".brain"))


def _registry() -> RegistryStore:
    return RegistryStore(Path(".brain/workspace"))


def _require_repository(repository_id: str, *capabilities: Capability):
    record = _registry().get(repository_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Repository '{repository_id}' not found")
    if record.disabled:
        raise HTTPException(status_code=409, detail=f"Repository '{repository_id}' is disabled")
    for capability in capabilities:
        if capability.value not in record.capabilities:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Repository '{repository_id}' lacks capability '{capability.value}'"
                ),
            )
    return record


class ValidateRequest(BaseModel):
    """A candidate patch to validate. Trust never comes from the request body."""

    repository_id: str
    patch_content: str = Field(min_length=1)
    patch_id: Optional[str] = None
    base_revision: Optional[str] = None
    profile_name: str = "python-compile"
    allowed_paths: List[str] = Field(default_factory=list)
    expected_file_hashes: dict = Field(default_factory=dict)


@router.get("/profiles", dependencies=[Depends(require_scope("lab:read"))])
async def list_profiles():
    """Declarative validation profiles available to the laboratory."""
    return {"status": "success", "profiles": _laboratory().list_profiles()}


@router.get("/workspaces", dependencies=[Depends(require_scope("lab:read"))])
async def list_workspaces():
    lab = _laboratory()
    return {
        "status": "success",
        "managed_root": str(lab.manager.managed_root),
        "workspaces": [w.to_dict() for w in lab.manager.list_workspaces()],
    }


@router.get("/workspaces/{workspace_id}", dependencies=[Depends(require_scope("lab:read"))])
async def get_workspace(workspace_id: str):
    record = _laboratory().manager.get_workspace(workspace_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Workspace '{workspace_id}' not found")
    return {"status": "success", "workspace": record.to_dict()}


@router.post("/validate", dependencies=[Depends(require_scope("lab:write"))])
async def validate_patch(req: ValidateRequest):
    """Validate a candidate patch inside a fresh disposable workspace."""
    record = _require_repository(
        req.repository_id,
        Capability.CREATE_WORKTREES,
        Capability.APPLY_CANDIDATE_PATCHES,
        Capability.EXECUTE_VALIDATION,
    )
    if len(req.patch_content.encode("utf-8", errors="replace")) > MAX_PATCH_BYTES:
        raise HTTPException(
            status_code=413, detail=f"Patch exceeds {MAX_PATCH_BYTES} bytes"
        )

    lab = _laboratory()
    base_revision = req.base_revision or record.current_revision
    patch = PatchRecord(
        patch_id=req.patch_id or "",
        source_type=PatchSource.UNIFIED_DIFF.value,
        repository_id=record.repository_id,
        base_revision=base_revision,
        patch_content=req.patch_content,
        allowed_paths=list(req.allowed_paths),
        expected_file_hashes=dict(req.expected_file_hashes),
        max_bytes=MAX_PATCH_BYTES,
    )
    session = lab.run_session(
        repository_id=record.repository_id,
        repo_path=Path(record.canonical_root),
        base_revision=base_revision,
        patch=patch,
        profile_name=req.profile_name,
        cleanup=True,
        allowed_profiles=record.allowed_validation_profiles or None,
    )
    return {"status": "success", "session": session}


@router.get("/sessions/{session_id}", dependencies=[Depends(require_scope("lab:read"))])
async def get_session(session_id: str):
    session = _laboratory().get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    return {"status": "success", "session": session}


@router.post("/workspaces/{workspace_id}/cleanup", dependencies=[Depends(require_scope("lab:write"))])
async def cleanup_workspace(workspace_id: str):
    """Dispose of a workspace. Quarantines instead of deleting when ownership
    cannot be proven."""
    report = _laboratory().manager.clean_workspace(workspace_id, reason="api cleanup")
    if not report.removed and not report.quarantined:
        raise HTTPException(status_code=404, detail=report.reason)
    return {"status": "success", "cleanup": report.to_dict()}


@router.post("/recover", dependencies=[Depends(require_scope("lab:write"))])
async def recover_orphans():
    """Expire due workspaces and reconcile orphan directories."""
    lab = _laboratory()
    expired = lab.manager.expire_due()
    report = lab.manager.recover_orphans()
    return {"status": "success", "expired_workspaces": expired, "recovery": report.to_dict()}
