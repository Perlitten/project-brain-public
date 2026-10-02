"""REST API Router for freshness and incremental intelligence — Phase C6.

Repository roots are resolved server-side from the registry; the caller never
supplies a filesystem path.
"""

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from apps.api.auth import require_api_key, require_scope
from brain.freshness.generation_deriver import GenerationDeriver
from brain.freshness.incremental_planner import IncrementalPlanner
from brain.freshness.models import ArtifactType, FreshnessState, RebuildDecision
from brain.freshness.tracker import FreshnessTracker
from brain.graph.generation_manager import GraphGenerationManager
from brain.workspace.models import Capability
from brain.workspace.registry_store import RegistryStore

router = APIRouter(
    prefix="/freshness",
    tags=["freshness"],
    dependencies=[Depends(require_api_key), Depends(require_scope("freshness:read"))],
)


def _registry() -> RegistryStore:
    return RegistryStore(Path(".brain/workspace"))


def _tracker() -> FreshnessTracker:
    return FreshnessTracker(Path(".brain/freshness"))


def _resolve_repository(repository_id: str):
    record = _registry().get(repository_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Repository '{repository_id}' not found")
    if record.disabled:
        raise HTTPException(status_code=409, detail=f"Repository '{repository_id}' is disabled")
    return record


class RebuildRequest(BaseModel):
    base_revision: Optional[str] = None
    candidate_revision: Optional[str] = None
    full: bool = False
    activate: bool = True


@router.get("/repositories/{repository_id}", dependencies=[Depends(require_scope("freshness:read"))])
async def repository_freshness(repository_id: str):
    """Freshness of a repository's graph plus every tracked artifact."""
    record = _resolve_repository(repository_id)
    repo_path = Path(record.canonical_root)
    mgr = GraphGenerationManager(repo_path / ".brain")

    observed = FreshnessTracker.resolve_revision(repo_path)
    active_gen = mgr.get_active_generation_id()
    active_meta = next(
        (g for g in mgr.list_generations() if g.generation_id == active_gen), None
    )

    graph_state = FreshnessState.UNKNOWN.value
    if active_meta and observed:
        graph_state = (
            FreshnessState.CURRENT.value
            if active_meta.git_revision == observed
            else FreshnessState.STALE.value
        )

    return {
        "status": "success",
        "repository_id": record.repository_id,
        "observed_revision": observed or None,
        "active_generation_id": active_gen,
        "graph_freshness": graph_state,
        "artifacts": [r.to_dict() for r in _tracker().list_by_repository(record.repository_id)],
    }


@router.get("/artifacts/{artifact_id}", dependencies=[Depends(require_scope("freshness:read"))])
async def artifact_freshness(artifact_id: str):
    """Evidence-based explanation of one artifact's freshness."""
    result = _tracker().explain(artifact_id)
    if not result.get("found"):
        raise HTTPException(status_code=404, detail=f"Artifact '{artifact_id}' not tracked")
    return {"status": "success", "artifact": result}


@router.post("/repositories/{repository_id}/rebuild", dependencies=[Depends(require_scope("freshness:write"))])
async def rebuild_repository(repository_id: str, req: RebuildRequest):
    """Derive a new graph generation. Human-authorized operational action.

    Requires the repository to hold the BUILD_GRAPH capability — a repository
    registered read-only cannot be rebuilt through the API.
    """
    record = _resolve_repository(repository_id)
    if Capability.BUILD_GRAPH.value not in record.capabilities:
        raise HTTPException(
            status_code=403,
            detail=f"Repository '{repository_id}' lacks capability '{Capability.BUILD_GRAPH.value}'",
        )

    repo_path = Path(record.canonical_root)
    mgr = GraphGenerationManager(repo_path / ".brain")
    tracker = _tracker()

    active_gen = mgr.get_active_generation_id()
    active_meta = next(
        (g for g in mgr.list_generations() if g.generation_id == active_gen), None
    )
    candidate = (
        req.candidate_revision or FreshnessTracker.resolve_revision(repo_path) or "HEAD"
    )
    base = req.base_revision or (active_meta.git_revision if active_meta else "")

    artifact_id = f"graph:{record.repository_id}"
    tracker.mark_building(artifact_id, caused_by="api rebuild")

    planner = IncrementalPlanner()
    plan = planner.plan_update(
        repo_path=repo_path,
        base_revision=base or candidate,
        candidate_revision=candidate,
        repository_id=record.repository_id,
        base_store=mgr.get_active_graph_store() if active_gen else None,
    )
    if req.full or not active_gen:
        plan.decision = RebuildDecision.FULL_REBUILD.value
        plan.fallback_reason = plan.fallback_reason or (
            "operator requested full rebuild" if req.full else "no active generation"
        )

    report = GenerationDeriver(repo_path, repository_id=record.repository_id).derive(
        plan, base_generation_id=active_gen, activate=req.activate
    )

    if report.validation_errors:
        tracker.mark_failed(artifact_id, "; ".join(report.validation_errors)[:400])
    else:
        tracker.record(
            artifact_id=artifact_id,
            artifact_type=ArtifactType.REPOSITORY_GRAPH.value,
            repository_id=record.repository_id,
            state=FreshnessState.CURRENT,
            source_revision=report.candidate_revision,
            observed_revision=FreshnessTracker.resolve_revision(repo_path),
            evidence=f"generation {report.new_generation_id} ({report.decision})",
        )

    return {
        "status": "error" if report.validation_errors else "success",
        "plan": plan.to_dict(),
        "derivation": report.to_dict(),
    }
