"""REST API Router for Assisted Remediation Plans."""

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from apps.api.auth import require_api_key, require_scope
from brain.config.paths import resolve_repo_path, validate_secure_repo_path
from brain.insights.remediation_manager import RemediationPlanManager
from brain.insights.remediation_strategies import RemediationStrategyRegistry

router = APIRouter(prefix="/insights/remediation", tags=["remediation-planner"], dependencies=[Depends(require_api_key), Depends(require_scope("remediation:read"))])


def _safe_resolve(repo_path_arg: Optional[str]) -> Path:
    if repo_path_arg:
        return validate_secure_repo_path(repo_path_arg)
    return resolve_repo_path()


@router.post("/plans", dependencies=[Depends(require_scope("remediation:write"))])
async def create_remediation_plan(
    rule_id: str = Query("DRIFT-001"),
    file_path: str = Query(...),
    description: str = Query("Architectural finding detected"),
    repo_path: Optional[str] = Query(None),
):
    target_repo = _safe_resolve(repo_path)
    mgr = RemediationPlanManager(target_repo / ".brain")

    finding_dict = {"rule_id": rule_id, "file_path": file_path, "description": description}
    plan = RemediationStrategyRegistry.create_plan_for_finding(target_repo.name, "HEAD", finding_dict, target_repo)
    saved = mgr.save_plan(plan)

    return {"status": "success", "plan": saved.to_dict()}


@router.get("/plans", dependencies=[Depends(require_scope("remediation:read"))])
async def list_remediation_plans(
    state: Optional[str] = Query(None),
    repo_path: Optional[str] = Query(None),
):
    target_repo = _safe_resolve(repo_path)
    mgr = RemediationPlanManager(target_repo / ".brain")
    plans = mgr.list_plans(state=state)
    return {"status": "success", "total_plans": len(plans), "plans": [p.to_dict() for p in plans]}


@router.get("/plans/{plan_id}", dependencies=[Depends(require_scope("remediation:read"))])
async def get_remediation_plan(plan_id: str, repo_path: Optional[str] = Query(None)):
    target_repo = _safe_resolve(repo_path)
    mgr = RemediationPlanManager(target_repo / ".brain")
    plan = mgr.get_plan(plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail=f"Remediation plan '{plan_id}' not found")

    fresh, reason = mgr.validate_plan_freshness(plan_id, target_repo)
    plan_dict = plan.to_dict()
    plan_dict["freshness_check"] = {"fresh": fresh, "reason": reason}

    return {"status": "success", "plan": plan_dict}


@router.post("/plans/{plan_id}/acknowledge", dependencies=[Depends(require_scope("remediation:write"))])
async def acknowledge_plan(plan_id: str, repo_path: Optional[str] = Query(None)):
    target_repo = _safe_resolve(repo_path)
    mgr = RemediationPlanManager(target_repo / ".brain")
    plan = mgr.update_state(plan_id, "acknowledged")
    if not plan:
        raise HTTPException(status_code=404, detail=f"Plan '{plan_id}' not found")
    return {"status": "success", "plan": plan.to_dict()}


@router.post("/plans/{plan_id}/approve", dependencies=[Depends(require_scope("remediation:write"))])
async def approve_plan(plan_id: str, repo_path: Optional[str] = Query(None)):
    target_repo = _safe_resolve(repo_path)
    mgr = RemediationPlanManager(target_repo / ".brain")
    plan = mgr.update_state(plan_id, "approved")
    if not plan:
        raise HTTPException(status_code=404, detail=f"Plan '{plan_id}' not found")
    return {"status": "success", "plan": plan.to_dict()}


@router.post("/plans/{plan_id}/reject", dependencies=[Depends(require_scope("remediation:write"))])
async def reject_plan(plan_id: str, repo_path: Optional[str] = Query(None)):
    target_repo = _safe_resolve(repo_path)
    mgr = RemediationPlanManager(target_repo / ".brain")
    plan = mgr.update_state(plan_id, "rejected")
    if not plan:
        raise HTTPException(status_code=404, detail=f"Plan '{plan_id}' not found")
    return {"status": "success", "plan": plan.to_dict()}


@router.post("/plans/{plan_id}/mark-implemented", dependencies=[Depends(require_scope("remediation:write"))])
async def mark_plan_implemented(plan_id: str, repo_path: Optional[str] = Query(None)):
    target_repo = _safe_resolve(repo_path)
    mgr = RemediationPlanManager(target_repo / ".brain")
    plan = mgr.update_state(plan_id, "implemented")
    if not plan:
        raise HTTPException(status_code=404, detail=f"Plan '{plan_id}' not found")
    return {"status": "success", "plan": plan.to_dict()}


@router.post("/plans/{plan_id}/verify", dependencies=[Depends(require_scope("remediation:write"))])
async def verify_plan(plan_id: str, repo_path: Optional[str] = Query(None)):
    target_repo = _safe_resolve(repo_path)
    mgr = RemediationPlanManager(target_repo / ".brain")
    plan = mgr.update_state(plan_id, "verified")
    if not plan:
        raise HTTPException(status_code=404, detail=f"Plan '{plan_id}' not found")
    return {"status": "success", "plan": plan.to_dict()}
