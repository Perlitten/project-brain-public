"""REST API Router for Deep Change-Impact Analysis."""

from pathlib import Path
from typing import Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from apps.api.auth import require_api_key, require_principal, require_scope
from apps.api.team_access import repository_access, repository_authority
from brain.auth.principals import ResolvedPrincipal
from brain.team.authorization import Permission
from brain.config.paths import resolve_repo_path, validate_secure_repo_path
from brain.insights.impact_engine import ImpactAnalysisEngine
from brain.insights.impact_models import ImpactResult

router = APIRouter(prefix="/insights/impact", tags=["change-impact"], dependencies=[Depends(require_api_key), Depends(require_scope("impact:read")),
                                  Depends(repository_access(Permission.READ))])

_IMPACT_CACHE: Dict[str, ImpactResult] = {}
# The repository each cached run analyzed (the ``repo_path`` it was requested with; None is the default one).
_IMPACT_REPOSITORY: Dict[str, Optional[str]] = {}


def _safe_resolve(repo_path_arg: Optional[str]) -> Path:
    if repo_path_arg:
        return validate_secure_repo_path(repo_path_arg)
    return resolve_repo_path()


@router.post("/analyze", dependencies=[Depends(require_scope("impact:write"))])
async def analyze_change_impact(
    base_rev: str = Query("HEAD~1"),
    candidate_rev: str = Query("HEAD"),
    max_depth: int = Query(3),
    repo_path: Optional[str] = Query(None),
):
    target_repo = _safe_resolve(repo_path)
    engine = ImpactAnalysisEngine(target_repo)
    result = engine.analyze_impact(base_rev=base_rev, cand_rev=candidate_rev, max_depth=max_depth)

    _IMPACT_CACHE[result.run_id] = result
    _IMPACT_REPOSITORY[result.run_id] = repo_path

    return {"status": "success", "result": result.to_dict()}


async def _cached_run(run_id: str, principal: ResolvedPrincipal) -> ImpactResult:
    """The cached run, if the caller may read the repository it analyzed; otherwise the same 404 as a missing run."""
    if run_id not in _IMPACT_CACHE:
        raise HTTPException(status_code=404, detail=f"Impact run ID '{run_id}' not found")
    try:
        await repository_authority(principal, Permission.READ, repo_path=_IMPACT_REPOSITORY.get(run_id))
    except HTTPException as exc:
        raise HTTPException(status_code=404, detail=f"Impact run ID '{run_id}' not found") from exc
    return _IMPACT_CACHE[run_id]


@router.get("/{run_id}", dependencies=[Depends(require_scope("impact:read"))])
async def get_impact_run(run_id: str, principal: ResolvedPrincipal = Depends(require_principal)):
    await _cached_run(run_id, principal)
    return {"status": "success", "result": _IMPACT_CACHE[run_id].to_dict()}


@router.get("/{run_id}/tests", dependencies=[Depends(require_scope("impact:read"))])
async def get_impacted_tests(run_id: str, principal: ResolvedPrincipal = Depends(require_principal)):
    res = await _cached_run(run_id, principal)
    return {
        "status": "success",
        "run_id": run_id,
        "suggested_tests": res.suggested_tests,
    }


@router.get("/{run_id}/paths", dependencies=[Depends(require_scope("impact:read"))])
async def get_impact_traversal_paths(run_id: str, principal: ResolvedPrincipal = Depends(require_principal)):
    res = await _cached_run(run_id, principal)
    paths = [{"entity_id": e.entity_id, "path": e.traversal_path} for e in res.impacted_entities]
    return {"status": "success", "run_id": run_id, "traversal_paths": paths}
