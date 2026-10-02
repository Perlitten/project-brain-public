"""RESTful API Router for Continuous Improvement Control Plane, AgentBundles, and Promotion in v0.9.0."""

from typing import List, Optional
from fastapi import APIRouter, Depends, Header, HTTPException, status
from apps.api.auth import require_api_key, require_scope
from brain._version import __version__
from brain.improvement.promotion import LexicographicPromotionEngine
from brain.improvement.registry.registry import BundleRegistry
from brain.improvement.safety_mutations import SafetyMutationAuditor
from brain.improvement.storage.db_store import RelationalControlPlaneStore
from brain.improvement.tournament.engine import MultiAgentTournamentEngine
from brain.improvement.weekly_factory import WeeklyImprovementFactory

router = APIRouter(prefix="/improvement", tags=["improvement"])

control_store = RelationalControlPlaneStore()
registry = BundleRegistry()
gate_engine = LexicographicPromotionEngine(registry)
safety_auditor = SafetyMutationAuditor()
weekly_factory = WeeklyImprovementFactory(registry)


@router.get("/status", dependencies=[Depends(require_scope("improvement:read"))])
def get_improvement_status(authenticated: bool = Depends(require_api_key)):
    all_killed, killed, total, failures = safety_auditor.audit_safety_mutations()
    return {
        "status": "FACTORY_OPERATIONAL_HUMAN_GATED",
        "version": __version__,
        "champion_bundle_id": registry.get_alias("champion"),
        "previous_champion_bundle_id": registry.get_alias("previous_champion"),
        "challenger_bundle_id": registry.get_alias("challenger"),
        "safety_mutation_kill_rate_pct": (killed / total) * 100.0 if total > 0 else 100.0,
        "safety_mutations_killed": f"{killed}/{total}",
        "training_eligible_default": False,
        "system_classification": "L1",
    }


@router.post("/factory/run-weekly", dependencies=[Depends(require_scope("improvement:write"))])
def run_weekly_factory_cycle(
    target_cluster_id: str = "cluster-premature-edit",
    authenticated: bool = Depends(require_api_key),
):
    """Triggers the weekly continuous improvement factory cycle."""
    factory = WeeklyImprovementFactory(registry)
    return factory.run_weekly_cycle(target_cluster_id=target_cluster_id)


@router.post("/tournaments", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_scope("improvement:write"))])
def create_multi_agent_tournament(
    target_cluster_ids: Optional[List[str]] = None,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    authenticated: bool = Depends(require_api_key),
):
    """Triggers an automated multi-candidate champion-anchored tournament league run."""
    clusters = target_cluster_ids or ["cluster-premature-edit", "cluster-context-overflow"]
    engine = MultiAgentTournamentEngine(registry)
    result = engine.run_tournament(target_cluster_ids=clusters)
    return result.model_dump()


@router.post("/tournament/run", dependencies=[Depends(require_scope("improvement:write"))])
def legacy_run_multi_agent_tournament(
    target_cluster_ids: Optional[List[str]] = None,
    authenticated: bool = Depends(require_api_key),
):
    """Legacy endpoint for backward compatibility."""
    return create_multi_agent_tournament(target_cluster_ids=target_cluster_ids, authenticated=authenticated)


@router.get("/trajectories/{trajectory_id}", dependencies=[Depends(require_scope("improvement:read"))])
def get_trajectory(trajectory_id: str, authenticated: bool = Depends(require_api_key)):
    t = control_store.get_trajectory_header(trajectory_id)
    if not t:
        raise HTTPException(status_code=404, detail="Trajectory not found")
    return t.model_dump()
