"""Task-ledger API for the Claude Code harness (P1 hardening, 2026-07-07).

Source of truth for orchestration state — see AGENT_DEPLOYMENT.md sibling
doc ~/.claude/harness/README.md on the harness side for the dispatcher CLI
that calls these endpoints.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query

from apps.api.auth import require_api_key, require_scope
from apps.api.harness_schemas import (
    AcceptanceCheckpointRequest,
    AcceptanceFinalizeRequest,
    ArtifactCreateRequest,
    EventCreateRequest,
    HandoffCreateRequest,
    LeaseAcquireRequest,
    TaskCreateRequest,
    TaskStatusUpdateRequest,
    ValidationCreateRequest,
)
from brain.database.harness_models import (
    AgentTask,
    AgentTaskArtifact,
    AgentTaskEvent,
    AgentValidationResult,
    MemoryHandoff,
    WorkerLease,
)
from brain.memory.harness_store import (
    AcceptanceLocked,
    AcceptanceAttestationInvalid,
    AcceptanceUnavailable,
    ActiveBlockers,
    HarnessStore,
    IdempotencyConflict,
    InvalidTransition,
    LeaseConflict,
    TaskNotFound,
    VersionConflict,
)

router = APIRouter(prefix="/harness", tags=["harness"], dependencies=[Depends(require_api_key), Depends(require_scope("harness:read"))])
PROTECTED_DECISION_EVENTS = frozenset(
    {
        "accepted",
        "rejected",
        "merge_ready",
        "merge_completed",
        "lifecycle_closed",
        "promotion_approved",
        "promotion_rejected",
    }
)


def _parse_task_id(task_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="task_id must be a UUID")


def _task_dict(t: AgentTask) -> dict:
    return {
        "id": str(t.id),
        "title": t.title,
        "goal": t.goal,
        "status": t.status,
        "owner_agent": t.owner_agent,
        "target_agent": t.target_agent,
        "repo_path": t.repo_path,
        "worktree_path": t.worktree_path,
        "created_by_session_id": t.created_by_session_id,
        "current_session_id": t.current_session_id,
        "priority": t.priority,
        "timeout_seconds": t.timeout_seconds,
        "retry_budget": t.retry_budget,
        "version": t.version,
        "cancellation_requested_at": t.cancellation_requested_at.isoformat() if t.cancellation_requested_at else None,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }


def _event_dict(e: AgentTaskEvent) -> dict:
    return {
        "id": e.id,
        "task_id": str(e.task_id),
        "event_type": e.event_type,
        "actor": e.actor,
        "payload": e.payload_json,
        "classification": e.classification,
        "idempotency_key": e.idempotency_key,
        "causal_parent_id": e.causal_parent_id,
        "expected_task_version": e.expected_task_version,
        "task_version": e.task_version,
        "created_at": e.created_at.isoformat() if e.created_at else None,
    }


def _artifact_dict(a: AgentTaskArtifact) -> dict:
    return {
        "id": a.id,
        "task_id": str(a.task_id),
        "kind": a.kind,
        "path_or_uri": a.path_or_uri,
        "checksum": a.checksum,
        "content_type": a.content_type,
        "bytes": a.bytes,
        "compressed_bytes": a.compressed_bytes,
        "redaction_status": a.redaction_status,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


def _validation_dict(v: AgentValidationResult) -> dict:
    return {
        "id": v.id,
        "task_id": str(v.task_id),
        "validator": v.validator,
        "command": v.command,
        "status": v.status,
        "exit_code": v.exit_code,
        "artifact_id": v.artifact_id,
        "metrics": v.metrics_json,
        "created_at": v.created_at.isoformat() if v.created_at else None,
    }


def _handoff_dict(h: MemoryHandoff) -> dict:
    return {
        "id": h.id,
        "task_id": str(h.task_id) if h.task_id else None,
        "session_id": h.session_id,
        "summary": h.summary,
        "memory_ref": h.memory_ref,
        "artifact_refs": h.artifact_refs,
        "created_at": h.created_at.isoformat() if h.created_at else None,
    }


def _lease_dict(lease: WorkerLease) -> dict:
    return {
        "id": lease.id,
        "task_id": str(lease.task_id),
        "worker_name": lease.worker_name,
        "lease_started_at": lease.lease_started_at.isoformat() if lease.lease_started_at else None,
        "lease_expires_at": lease.lease_expires_at.isoformat() if lease.lease_expires_at else None,
        "heartbeat_at": lease.heartbeat_at.isoformat() if lease.heartbeat_at else None,
        "fencing_token": lease.fencing_token,
    }


@router.post("/tasks", dependencies=[Depends(require_scope("harness:write"))])
async def create_task(body: TaskCreateRequest):
    task = await HarnessStore.create_task(
        title=body.title,
        goal=body.goal,
        repo_path=body.repo_path,
        owner_agent=body.owner_agent,
        target_agent=body.target_agent,
        worktree_path=body.worktree_path,
        created_by_session_id=body.created_by_session_id,
        priority=body.priority,
        timeout_seconds=body.timeout_seconds,
        retry_budget=body.retry_budget,
    )
    return _task_dict(task)


@router.get("/tasks", dependencies=[Depends(require_scope("harness:read"))])
async def list_tasks(status: str | None = None, repo_path: str | None = None,
                     q: str | None = Query(None, max_length=500),
                     page: int = Query(1, ge=1), page_size: int | None = Query(None, ge=1, le=500),
                     limit: int | None = Query(None, ge=1, le=500), display_status: str | None = Query(None, max_length=50)):
    safe_limit = page_size if isinstance(page_size, int) else limit if isinstance(limit, int) else 100
    safe_page = page if isinstance(page, int) else 1
    q = q if isinstance(q, str) else None
    status_filter = status
    if display_status == "running":
        status_filter = "__running__"
    tasks = await HarnessStore.list_tasks(status=status_filter, repo_path=repo_path, query=q,
                                          offset=(safe_page - 1) * safe_limit, limit=safe_limit)
    total = await HarnessStore.count_tasks(status=status_filter, repo_path=repo_path, query=q)
    facets = await HarnessStore.task_status_counts(repo_path=repo_path, query=q)
    return {"tasks": [_task_dict(t) for t in tasks], "page": safe_page, "page_size": safe_limit,
            "total": total, "total_pages": max(1, (total + safe_limit - 1) // safe_limit),
            "facets": {"status": facets}, "scope": "repository" if repo_path else "global_or_unscoped"}


@router.get("/tasks/{task_id}", dependencies=[Depends(require_scope("harness:read"))])
async def get_task(task_id: str):
    task = await HarnessStore.get_task(_parse_task_id(task_id))
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return _task_dict(task)


@router.patch("/tasks/{task_id}", dependencies=[Depends(require_scope("harness:write"))])
async def update_task_status(task_id: str, body: TaskStatusUpdateRequest):
    if body.status == "passed":
        raise HTTPException(
            status_code=403,
            detail="passed is a protected acceptance state",
        )
    try:
        task = await HarnessStore.update_status(
            _parse_task_id(task_id),
            new_status=body.status,
            actor=body.actor,
            expected_task_version=body.expected_task_version,
            current_session_id=body.current_session_id,
            note=body.note,
        )
    except TaskNotFound:
        raise HTTPException(status_code=404, detail="task not found")
    except InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except VersionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return _task_dict(task)


@router.post("/tasks/{task_id}/events", dependencies=[Depends(require_scope("harness:write"))])
async def add_event(task_id: str, body: EventCreateRequest):
    if body.event_type in PROTECTED_DECISION_EVENTS:
        raise HTTPException(
            status_code=403,
            detail="protected lifecycle decisions require the acceptance authority",
        )
    try:
        event = await HarnessStore.add_event(
            _parse_task_id(task_id),
            body.event_type,
            body.actor,
            body.payload,
            classification=body.classification,
            idempotency_key=body.idempotency_key,
            causal_parent_id=body.causal_parent_id,
            expected_task_version=body.expected_task_version,
        )
    except TaskNotFound:
        raise HTTPException(status_code=404, detail="task not found")
    except VersionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except AcceptanceLocked as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return _event_dict(event)


@router.post("/tasks/{task_id}/acceptance-checkpoints/{checkpoint_id}/finalize", dependencies=[Depends(require_scope("harness:write"))])
async def finalize_acceptance_checkpoint(
    task_id: str,
    checkpoint_id: int,
    body: AcceptanceFinalizeRequest,
):
    try:
        event = await HarnessStore.finalize_acceptance_checkpoint(
            _parse_task_id(task_id),
            checkpoint_id=checkpoint_id,
            expected_task_version=body.expected_task_version,
            acceptance_record=body.acceptance_record.model_dump(),
        )
    except TaskNotFound:
        raise HTTPException(status_code=404, detail="task not found")
    except AcceptanceUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except (
        VersionConflict,
        IdempotencyConflict,
        AcceptanceLocked,
        AcceptanceAttestationInvalid,
        InvalidTransition,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return _event_dict(event)


@router.get("/tasks/{task_id}/events", dependencies=[Depends(require_scope("harness:read"))])
async def list_events(task_id: str, limit: int = 200, after_id: int | None = None):
    if not 1 <= limit <= 1000:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 1000")
    events = await HarnessStore.list_events(_parse_task_id(task_id), limit=limit, after_id=after_id)
    return {"events": [_event_dict(e) for e in events]}


@router.post("/tasks/{task_id}/acceptance-checkpoints", dependencies=[Depends(require_scope("harness:write"))])
async def add_acceptance_checkpoint(
    task_id: str,
    body: AcceptanceCheckpointRequest,
):
    try:
        event = await HarnessStore.add_acceptance_checkpoint(
            _parse_task_id(task_id),
            attempt_id=body.attempt_id,
            authority=body.authority,
            idempotency_key=body.idempotency_key,
            expected_task_version=body.expected_task_version,
            evidence={
                "external_task_id": body.external_task_id,
                "contract_sha256": body.contract_sha256,
                "gate_report_sha256": body.gate_report_sha256,
                "ledger_sha256": body.ledger_sha256,
                "brain_context_sha256": body.brain_context_sha256,
                "result_commit": body.result_commit,
                "repository_fingerprint": body.repository_fingerprint,
                "fencing_token": body.fencing_token,
            },
        )
    except TaskNotFound:
        raise HTTPException(status_code=404, detail="task not found")
    except AcceptanceUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except (
        VersionConflict,
        IdempotencyConflict,
        ActiveBlockers,
        AcceptanceLocked,
        InvalidTransition,
        LeaseConflict,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return _event_dict(event)


@router.post("/tasks/{task_id}/artifacts", dependencies=[Depends(require_scope("harness:write"))])
async def add_artifact(task_id: str, body: ArtifactCreateRequest):
    try:
        artifact = await HarnessStore.add_artifact(
            _parse_task_id(task_id),
            kind=body.kind,
            path_or_uri=body.path_or_uri,
            checksum=body.checksum,
            content_type=body.content_type,
            size_bytes=body.size_bytes,
            compressed_bytes=body.compressed_bytes,
            redaction_status=body.redaction_status,
        )
    except TaskNotFound:
        raise HTTPException(status_code=404, detail="task not found")
    return _artifact_dict(artifact)


@router.get("/tasks/{task_id}/artifacts", dependencies=[Depends(require_scope("harness:read"))])
async def list_artifacts(task_id: str):
    artifacts = await HarnessStore.list_artifacts(_parse_task_id(task_id))
    return {"artifacts": [_artifact_dict(a) for a in artifacts]}


@router.post("/tasks/{task_id}/validations", dependencies=[Depends(require_scope("harness:write"))])
async def add_validation(task_id: str, body: ValidationCreateRequest):
    try:
        validation = await HarnessStore.add_validation(
            _parse_task_id(task_id),
            validator=body.validator,
            status=body.status,
            command=body.command,
            exit_code=body.exit_code,
            artifact_id=body.artifact_id,
            metrics=body.metrics,
        )
    except TaskNotFound:
        raise HTTPException(status_code=404, detail="task not found")
    return _validation_dict(validation)


@router.get("/tasks/{task_id}/validations", dependencies=[Depends(require_scope("harness:read"))])
async def list_validations(task_id: str):
    validations = await HarnessStore.list_validations(_parse_task_id(task_id))
    return {"validations": [_validation_dict(v) for v in validations]}


@router.post("/handoffs", dependencies=[Depends(require_scope("harness:write"))])
async def add_handoff(body: HandoffCreateRequest):
    handoff = await HarnessStore.add_handoff(
        session_id=body.session_id,
        summary=body.summary,
        task_id=body.task_id,
        memory_ref=body.memory_ref,
        artifact_refs=body.artifact_refs,
    )
    return _handoff_dict(handoff)


@router.get("/handoffs", dependencies=[Depends(require_scope("harness:read"))])
async def list_handoffs(session_id: str | None = None, task_id: str | None = None, limit: int = 20):
    tid = _parse_task_id(task_id) if task_id else None
    handoffs = await HarnessStore.list_handoffs(session_id=session_id, task_id=tid, limit=limit)
    return {"handoffs": [_handoff_dict(h) for h in handoffs]}


@router.post("/tasks/{task_id}/leases", dependencies=[Depends(require_scope("harness:write"))])
async def acquire_lease(task_id: str, body: LeaseAcquireRequest):
    try:
        lease = await HarnessStore.acquire_lease(_parse_task_id(task_id), body.worker_name, body.ttl_seconds)
    except TaskNotFound:
        raise HTTPException(status_code=404, detail="task not found")
    except LeaseConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return _lease_dict(lease)
