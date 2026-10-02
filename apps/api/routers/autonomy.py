"""REST API endpoints for Production GoalRun Service (v0.9.0)."""
import asyncio
from typing import Any, Dict, Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from apps.api.auth import require_api_key, require_scope
from brain.autonomy.goal_run import GoalRunService

router = APIRouter(prefix="/autonomy", tags=["autonomy"], dependencies=[Depends(require_api_key), Depends(require_scope("autonomy:read"))])
_goal_service = GoalRunService()


class GoalSubmitRequest(BaseModel):
    goal_id: str
    description: str
    target_file: Optional[str] = None
    oracle_test: Optional[str] = None
    requires_deploy: bool = False


class GoalCancelRequest(BaseModel):
    reason: str = "User requested cancellation"


@router.post("/goals", dependencies=[Depends(require_scope("autonomy:write"))])
async def submit_goal(req: GoalSubmitRequest) -> Dict[str, Any]:
    """Submit a natural language goal to the autonomous engineering work queue."""
    result = await _goal_service.submit_goal(
        goal_id=req.goal_id,
        description=req.description,
        target_file=req.target_file,
        oracle_test=req.oracle_test,
        requires_deploy=req.requires_deploy,
    )

    if result.get("status") == "BLOCKED":
        return {
            "status": "blocked",
            "goal_id": req.goal_id,
            "phase": "BLOCKED_ESCALATED",
            "detail": result.get("detail", "Goal blocked by safety policy"),
        }

    # Process in background task without blocking API HTTP response
    asyncio.create_task(_goal_service.execute_goal_run(req.goal_id))

    return {
        "status": "ok",
        "goal_id": req.goal_id,
        "phase": "GOAL_RECEIVED",
        "worktree_path": f"/app/context_packs/worktrees/{req.goal_id}",
    }


@router.get("/goals/{goal_id}", dependencies=[Depends(require_scope("autonomy:read"))])
async def get_goal_status(goal_id: str) -> Dict[str, Any]:
    """Get status, phase, checkpoints, and telemetry for a GoalRun."""
    run = _goal_service.get_goal_status(goal_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"GoalRun {goal_id} not found.")
    return {
        "goal_id": run.goal_id,
        "description": run.description,
        "phase": run.current_phase.value,
        "commit_sha": run.commit_sha,
        "deployed": run.deployed,
        "worktree_path": run.worktree_path,
    }


@router.post("/goals/{goal_id}/cancel", dependencies=[Depends(require_scope("autonomy:write"))])
async def cancel_goal(goal_id: str, req: GoalCancelRequest) -> Dict[str, Any]:
    """Cancel an active GoalRun."""
    success = _goal_service.cancel_goal(goal_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"GoalRun {goal_id} not found.")
    return {"status": "cancelled", "goal_id": goal_id, "reason": req.reason}
