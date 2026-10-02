"""REST API Router for the operator control plane — Phases G1–G4.

Read-only. The queue surfaces decisions for a human to make; there is no
endpoint that performs one of them, and none that acts on an authoritative
repository.
"""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query

from apps.api.auth import require_api_key, require_scope
from brain.control.budgets import BudgetExceededError, BudgetUnknownError
from brain.control.plane import ALLOWED_ACTIONS, ControlPlane

router = APIRouter(
    prefix="/control",
    tags=["control-plane"],
    dependencies=[Depends(require_api_key), Depends(require_scope("control:read"))],
)


def _plane() -> ControlPlane:
    return ControlPlane(Path(".brain"))


@router.get("/summary", dependencies=[Depends(require_scope("control:read"))])
async def summary():
    """Combined operator summary across every v0.5.0 subsystem."""
    return _plane().summary()


@router.get("/queue", dependencies=[Depends(require_scope("control:read"))])
async def queue(
    repository_id: str = Query("", max_length=128),
    action_type: str = Query("", max_length=64),
    max_priority: int = Query(0, ge=0, le=4),
):
    if action_type and action_type not in ALLOWED_ACTIONS:
        raise HTTPException(status_code=400, detail=f"Unknown action type: {action_type}")
    items = _plane().action_queue(repository_id=repository_id)
    if action_type:
        items = [i for i in items if i["action_type"] == action_type]
    if max_priority:
        items = [i for i in items if i["priority"] <= max_priority]
    return {"items": items, "total": len(items)}


@router.get("/actions", dependencies=[Depends(require_scope("control:read"))])
async def allowed_actions():
    """The queue vocabulary. Every action is a human decision."""
    return {
        "action_types": {k: list(v) for k, v in sorted(ALLOWED_ACTIONS.items())},
        "autonomous_execution": False,
    }


@router.get("/budgets", dependencies=[Depends(require_scope("control:read"))])
async def budgets():
    return _plane().budgets()


@router.get("/budgets/{budget}/check", dependencies=[Depends(require_scope("control:read"))])
async def check_budget(budget: str, requested: int = Query(1, ge=1, le=1000)):
    """Fail-closed admission check. Refusal is a 409, not a soft warning."""
    try:
        usage = _plane().check_budget(budget, requested)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown budget: {budget}")
    except BudgetExceededError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except BudgetUnknownError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"allowed": True, **usage.to_dict()}


@router.get("/metrics", dependencies=[Depends(require_scope("control:read"))])
async def metrics():
    return _plane().metrics()


@router.get("/health", dependencies=[Depends(require_scope("control:read"))])
async def health():
    return _plane().health()
