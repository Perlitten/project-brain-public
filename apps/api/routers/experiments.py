"""REST API Router for remediation experiments — Phase E7.

No endpoint here promotes a patch to an authoritative repository. `/export`
returns a package for a human to apply themselves; recording a human decision
is a workflow record, not an application.
"""

from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from apps.api.auth import require_api_key, require_scope
from brain.experiments.models import (
    ExperimentOption,
    ExportNotAuthorizedError,
    ExportStaleError,
    HumanAction,
)
from brain.experiments.orchestrator import ExperimentManager, ExperimentStore
from brain.lab.laboratory import ChangeLaboratory
from brain.workspace.models import Capability
from brain.workspace.registry_store import RegistryStore

router = APIRouter(
    prefix="/experiments",
    tags=["remediation-experiments"],
    dependencies=[Depends(require_api_key), Depends(require_scope("experiments:read"))],
)

MAX_PATCH_BYTES = 500_000
MAX_OPTIONS = 8


def _manager() -> ExperimentManager:
    brain_dir = Path(".brain")
    return ExperimentManager(
        ExperimentStore(brain_dir / "experiments"), ChangeLaboratory(brain_dir)
    )


def _require_repository(repository_id: str, *capabilities: Capability):
    record = RegistryStore(Path(".brain/workspace")).get(repository_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Repository '{repository_id}' not found")
    if record.disabled:
        raise HTTPException(status_code=409, detail=f"Repository '{repository_id}' is disabled")
    for capability in capabilities:
        if capability.value not in record.capabilities:
            raise HTTPException(
                status_code=403,
                detail=f"Repository '{repository_id}' lacks capability '{capability.value}'",
            )
    return record


def _require_experiment(manager: ExperimentManager, experiment_id: str):
    experiment = manager.get(experiment_id)
    if experiment is None:
        raise HTTPException(status_code=404, detail=f"Experiment '{experiment_id}' not found")
    return experiment


class OptionRequest(BaseModel):
    option_id: str = Field(min_length=1)
    candidate_patch: str = Field(min_length=1)
    patch_provenance: str = ""
    source_remediation_option: str = ""
    expected_files: List[str] = Field(default_factory=list)
    expected_effect: str = ""
    validation_profile: str = ""


class CreateExperimentRequest(BaseModel):
    """Trust is never taken from the body: capabilities come from the registry."""

    repository_id: str
    options: List[OptionRequest]
    base_revision: Optional[str] = None
    source_plan_id: str = ""
    source_finding_id: str = ""
    graph_generation: str = ""
    policy_version: str = ""
    validation_profile: str = "python-standard"
    max_parallel_options: int = Field(default=1, ge=1, le=8)
    retain_workspaces: bool = False


class HumanActionRequest(BaseModel):
    action: str
    actor: str = "api"
    option_id: str = ""
    note: str = ""


class ExportRequest(BaseModel):
    option_id: str
    authoritative_revision: Optional[str] = None
    revalidated: bool = False


@router.post("", dependencies=[Depends(require_scope("experiments:write"))])
async def create_experiment(req: CreateExperimentRequest):
    record = _require_repository(
        req.repository_id,
        Capability.CREATE_WORKTREES,
        Capability.APPLY_CANDIDATE_PATCHES,
        Capability.EXECUTE_VALIDATION,
    )
    if not req.options:
        raise HTTPException(status_code=422, detail="At least one option is required")
    if len(req.options) > MAX_OPTIONS:
        raise HTTPException(
            status_code=422, detail=f"At most {MAX_OPTIONS} options per experiment"
        )
    for option in req.options:
        if len(option.candidate_patch.encode("utf-8", errors="replace")) > MAX_PATCH_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Option '{option.option_id}' exceeds {MAX_PATCH_BYTES} bytes",
            )
    if len({o.option_id for o in req.options}) != len(req.options):
        raise HTTPException(status_code=422, detail="Option ids must be unique")

    experiment = _manager().create_experiment(
        repository_id=record.repository_id,
        base_revision=req.base_revision or record.current_revision,
        options=[ExperimentOption(**o.model_dump()) for o in req.options],
        source_plan_id=req.source_plan_id,
        source_finding_id=req.source_finding_id,
        graph_generation=req.graph_generation,
        policy_version=req.policy_version,
        creation_actor="api",
        validation_profile=req.validation_profile,
        max_parallel_options=req.max_parallel_options,
        retain_workspaces=req.retain_workspaces,
    )
    return {"status": "success", "experiment": experiment.to_dict()}


@router.get("", dependencies=[Depends(require_scope("experiments:read"))])
async def list_experiments(repository_id: Optional[str] = None):
    experiments = _manager().list_all()
    if repository_id:
        experiments = [e for e in experiments if e.repository_id == repository_id]
    return {
        "status": "success",
        "count": len(experiments),
        "experiments": [
            {
                "experiment_id": e.experiment_id,
                "repository_id": e.repository_id,
                "base_revision": e.base_revision,
                "state": e.state,
                "conclusion": e.conclusion,
                "recommended_option_id": e.recommended_option_id,
                "created_at_utc": e.created_at_utc,
            }
            for e in experiments
        ],
    }


@router.get("/{experiment_id}", dependencies=[Depends(require_scope("experiments:read"))])
async def get_experiment(experiment_id: str):
    manager = _manager()
    return {
        "status": "success",
        "experiment": _require_experiment(manager, experiment_id).to_dict(),
    }


@router.post("/{experiment_id}/run", dependencies=[Depends(require_scope("experiments:write"))])
async def run_experiment(experiment_id: str):
    manager = _manager()
    experiment = _require_experiment(manager, experiment_id)
    record = _require_repository(
        experiment.repository_id,
        Capability.CREATE_WORKTREES,
        Capability.APPLY_CANDIDATE_PATCHES,
        Capability.EXECUTE_VALIDATION,
    )
    updated = manager.run_experiment(
        experiment_id,
        Path(record.canonical_root),
        authoritative_revision=record.current_revision,
        allowed_profiles=record.allowed_validation_profiles or None,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Experiment '{experiment_id}' no longer exists")
    return {"status": "success", "experiment": updated.to_dict()}


@router.post("/{experiment_id}/cancel", dependencies=[Depends(require_scope("experiments:write"))])
async def cancel_experiment(experiment_id: str):
    manager = _manager()
    _require_experiment(manager, experiment_id)
    cancelled = manager.request_cancel(experiment_id)
    if cancelled is None:
        raise HTTPException(status_code=404, detail=f"Experiment '{experiment_id}' no longer exists")
    return {"status": "success", "experiment": cancelled.to_dict()}


@router.get("/{experiment_id}/comparison", dependencies=[Depends(require_scope("experiments:read"))])
async def get_comparison(experiment_id: str):
    manager = _manager()
    _require_experiment(manager, experiment_id)
    comparison = manager.get_comparison(experiment_id)
    if comparison is None:
        raise HTTPException(
            status_code=409, detail=f"Experiment '{experiment_id}' has not been compared yet"
        )
    return {"status": "success", "comparison": comparison.to_dict()}


@router.get("/{experiment_id}/recommendation", dependencies=[Depends(require_scope("experiments:read"))])
async def get_recommendation(experiment_id: str):
    manager = _manager()
    _require_experiment(manager, experiment_id)
    return {"status": "success", "recommendation": manager.get_recommendation(experiment_id)}


@router.post("/{experiment_id}/actions", dependencies=[Depends(require_scope("experiments:write"))])
async def record_action(experiment_id: str, req: HumanActionRequest):
    """Record a human workflow decision. Accepting an option does not apply it."""
    manager = _manager()
    _require_experiment(manager, experiment_id)
    if req.action not in {a.value for a in HumanAction}:
        raise HTTPException(status_code=422, detail=f"Unknown action '{req.action}'")
    try:
        experiment = manager.record_human_action(
            experiment_id,
            action=req.action,
            actor=req.actor,
            option_id=req.option_id,
            note=req.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if experiment is None:
        raise HTTPException(status_code=404, detail=f"Experiment '{experiment_id}' no longer exists")
    return {
        "status": "success",
        "applied": False,
        "human_actions": [a.to_dict() for a in experiment.human_actions],
    }


@router.post("/{experiment_id}/export", dependencies=[Depends(require_scope("experiments:write"))])
async def export_patch(experiment_id: str, req: ExportRequest):
    """Package an accepted patch for manual application. Nothing is written to
    the authoritative repository."""
    manager = _manager()
    experiment = _require_experiment(manager, experiment_id)
    record = _require_repository(experiment.repository_id, Capability.EXPORT_PATCH)
    try:
        export = manager.export_patch(
            experiment_id,
            req.option_id,
            authoritative_revision=req.authoritative_revision or record.current_revision,
            revalidated=req.revalidated,
        )
    except ExportNotAuthorizedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ExportStaleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "success", "applied_to_repository": False, "export": export.to_dict()}
