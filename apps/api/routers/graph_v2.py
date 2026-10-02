"""REST API Router for Graphify v2 Operations."""

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from apps.api.auth import require_api_key, require_scope
from brain.config.paths import resolve_repo_path, validate_secure_repo_path
from brain.graph.builder_v2 import GraphBuilderV2
from brain.graph.generation_manager import GraphGenerationManager

router = APIRouter(prefix="/insights/graph", tags=["graph-v2"], dependencies=[Depends(require_api_key), Depends(require_scope("graph:read"))])


def _safe_resolve(repo_path_arg: Optional[str]) -> Path:
    if repo_path_arg:
        return validate_secure_repo_path(repo_path_arg)
    return resolve_repo_path()


@router.get("/generations", dependencies=[Depends(require_scope("graph:read"))])
async def list_generations(repo_path: Optional[str] = Query(None)):
    target_repo = _safe_resolve(repo_path)
    mgr = GraphGenerationManager(target_repo / ".brain")
    gens = mgr.list_generations()
    active_id = mgr.get_active_generation_id()
    return {
        "status": "success",
        "repository": target_repo.as_posix(),
        "active_generation_id": active_id,
        "total_generations": len(gens),
        "generations": [g.to_dict() for g in gens],
    }


@router.post("/build", dependencies=[Depends(require_scope("graph:write"))])
async def build_generation(
    repo_path: Optional[str] = Query(None),
    git_revision: str = Query("HEAD"),
    activate: bool = Query(True),
):
    target_repo = _safe_resolve(repo_path)
    builder = GraphBuilderV2(target_repo)
    meta, report = builder.build_generation(git_revision=git_revision)

    if activate:
        mgr = GraphGenerationManager(target_repo / ".brain")
        mgr.activate_generation(meta.generation_id)

    return {
        "status": "success",
        "generation": meta.to_dict(),
        "quality_report": report.to_dict(),
        "is_active": activate,
    }


@router.post("/activate/{generation_id}", dependencies=[Depends(require_scope("graph:write"))])
async def activate_generation(generation_id: str, repo_path: Optional[str] = Query(None)):
    target_repo = _safe_resolve(repo_path)
    mgr = GraphGenerationManager(target_repo / ".brain")
    if mgr.activate_generation(generation_id):
        return {"status": "success", "active_generation_id": generation_id}
    raise HTTPException(status_code=400, detail=f"Cannot activate generation '{generation_id}'")


@router.post("/rollback", dependencies=[Depends(require_scope("graph:write"))])
async def rollback_generation(repo_path: Optional[str] = Query(None)):
    target_repo = _safe_resolve(repo_path)
    mgr = GraphGenerationManager(target_repo / ".brain")
    rolled_to = mgr.rollback()
    if rolled_to:
        return {"status": "success", "active_generation_id": rolled_to}
    raise HTTPException(status_code=400, detail="No prior valid generation available for rollback")
