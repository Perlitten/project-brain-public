"""API Router for Selective Routing in Project Brain v0.5.3."""

from fastapi import APIRouter, Depends
from brain.routing.models import RoutingContext, PolicyMode
from brain.routing.policy_engine import SelectivePolicyEngine
from apps.api.auth import require_api_key, require_scope

router = APIRouter(prefix="/routing", tags=["routing"])
engine = SelectivePolicyEngine()


@router.get("/policy", dependencies=[Depends(require_scope("routing:read"))])
def get_routing_policy(authenticated: bool = Depends(require_api_key)):
    return {
        "policy_version": engine.policy.policy_version,
        "mode": engine.policy.mode.value,
        "category_routes": {k: v.value for k, v in engine.policy.category_routes.items()},
    }


@router.get("/decisions", dependencies=[Depends(require_scope("routing:read"))])
def evaluate_routing_decision(
    task_category: str = "general",
    file_count: int = 1,
    authenticated: bool = Depends(require_api_key),
):
    ctx = RoutingContext(task_category=task_category, file_count=file_count, operator_mode=PolicyMode.OBSERVE)
    decision = engine.evaluate(ctx)
    return decision.model_dump()


@router.get("/summary", dependencies=[Depends(require_scope("routing:read"))])
def get_routing_summary(authenticated: bool = Depends(require_api_key)):
    return {
        "active_mode": PolicyMode.OBSERVE.value,
        "supported_routes_count": 8,
        "total_evaluations": 0,
    }
