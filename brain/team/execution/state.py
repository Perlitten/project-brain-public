"""Execution state derivation and lifecycle: request, read, cancel."""
from __future__ import annotations

import uuid
from datetime import datetime
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from brain.auth.principals import ResolvedPrincipal
from brain.database.harness_models import AgentTaskEvent
from brain.execution.bounded import ExecutorBackend
from brain.team import execution_llm
from brain.team._util import _iso, _now
from brain.team.authorization import (Permission, TeamConflict, TeamDenied, administrator, authorize, fresh, require_team_storage)
from brain.team.models import AgentRun, AgentRunExecution
from brain.team.store import digest
from brain.team.execution._common import (EVENT_PREFIX, ExecutionDenied, SessionFactory, TERMINAL, _event, _events, _run_and_row)
from brain.team.execution.artifacts import _artifact_list
from brain.team.execution.policy import (Capabilities, ExecutionPolicy, _check_steps, _normalize_request, authorize_execution, default_backend, operator_policy)


def derive_state(row: AgentRunExecution, events: list[AgentTaskEvent], now: datetime | None = None) -> str:
    for item in reversed(events):
        kind = item.event_type.removeprefix(EVENT_PREFIX)
        if kind in TERMINAL:
            return kind
    if row.started_at is None:
        return "requested"
    expires = row.lease_expires_at
    if row.released_at is None and expires is not None and expires > (now or _now()):
        return "running"
    return "orphaned"


async def execution_dict(session: AsyncSession, row: AgentRunExecution, run: AgentRun) -> dict:
    events = await _events(session, run)
    artifacts = await _artifact_list(session, run.id)
    return {
        "run_id": str(run.id), "task_id": str(run.task_id), "state": derive_state(row, events),
        "backend": row.backend, **Capabilities.from_row(row).as_dict(), "steps": row.steps,
        "steps_recorded": row.steps_done, "fencing_token": row.fencing_token,
        "lease_expires_at": _iso(row.lease_expires_at), "started_at": _iso(row.started_at),
        "cancel_requested_at": _iso(row.cancel_requested_at), "base_sha": run.base_sha, "head_sha": run.head_sha,
        "events": [{"type": item.event_type, "actor": item.actor, "at": _iso(item.created_at),
                    "payload": item.payload_json} for item in events],
        "artifacts": artifacts,
    }


async def request_execution(session: AsyncSession, caller: ResolvedPrincipal, project_id: uuid.UUID,
                            task_id: uuid.UUID, run_id: uuid.UUID, payload: dict, *,
                            policy: ExecutionPolicy | None = None, backend: ExecutorBackend | None = None) -> dict:
    """Record a bounded execution for a run after every gate; idempotent on key + content + run identity."""
    policy = policy or operator_policy()
    backend = backend or default_backend(policy)
    require_team_storage()
    reason = policy.disabled_reason()
    if reason:
        raise ExecutionDenied(reason)
    steps, requested, canonical = _normalize_request(payload)
    if requested.llm is not None:
        await execution_llm.lock_budget(session, project_id)  # serialise reservations against the budget
    run, authority, effective = await authorize_execution(session, caller, project_id, task_id, run_id,
                                                          policy=policy, backend=backend, capabilities=requested)
    _check_steps(steps, effective, policy)
    key = str(payload.get("idempotency_key") or "")
    if not key or len(key) > 128:
        raise ExecutionDenied("An idempotency key of at most 128 characters is required")
    fingerprint = digest({"run_id": str(run.id), "task_id": str(task_id), "backend": backend.name, **canonical})

    async def existing() -> dict | None:
        found = await fresh(session, AgentRunExecution, AgentRunExecution.run_id == run.id)
        if found is None:
            return None
        if found.request_key != key or found.request_digest != fingerprint:
            raise TeamConflict("This run already has an execution request with different content; request a new run")
        return await execution_dict(session, found, run)

    replay = await existing()
    if replay is not None:
        return replay
    from brain.team import coordination  # lazy: coordination builds on brain.team.store, as this module does

    await coordination.require_dependencies_done(session, task_id)
    try:
        async with session.begin_nested():
            row = AgentRunExecution(run_id=run.id, request_key=key, request_digest=fingerprint,
                                    requested_by=authority.actor_id, requested_credential_id=caller.credential_id,
                                    steps=steps, tools=sorted(effective.tools),
                                    secret_refs=sorted(effective.secret_refs), egress=sorted(effective.egress),
                                    budget=effective.budget, backend=backend.name,
                                    environment=effective.environment, llm=effective.llm, fencing_token=0,
                                    steps_done=0)
            session.add(row)
            _event(session, run, "requested", "requested", f"principal:{authority.actor_id}",
                   {**effective.as_dict(), "backend": backend.name,
                    "steps": [{"tool": s["tool"], "args": s["args"], "secrets": s["secrets"]} for s in steps]})
            await session.flush()
    except IntegrityError:
        replay = await existing()
        if replay is None:
            raise
        return replay
    return await execution_dict(session, row, run)


async def get_execution(session: AsyncSession, caller: ResolvedPrincipal, project_id: uuid.UUID,
                        task_id: uuid.UUID, run_id: uuid.UUID) -> dict:
    await authorize(session, caller, project_id, Permission.READ, task_id=task_id)
    run, row = await _run_and_row(session, task_id, run_id)
    return await execution_dict(session, row, run)


async def cancel_execution(session: AsyncSession, caller: ResolvedPrincipal, project_id: uuid.UUID,
                           task_id: uuid.UUID, run_id: uuid.UUID) -> dict:
    """Request cancellation. A queued execution is cancelled at once; a running one at its next checkpoint."""
    authority = await authorize(session, caller, project_id, Permission.READ, task_id=task_id)
    run, _ = await _run_and_row(session, task_id, run_id)
    if authority.actor_id != run.requested_by and authority.initiator_id != run.initiator_id:
        try:
            await administrator(session, caller, project_id)
        except TeamDenied as exc:
            raise ExecutionDenied("Only the requester, the initiator or a team administrator may cancel") from exc
    row = await fresh(session, AgentRunExecution, AgentRunExecution.run_id == run_id, exclusive=True)
    assert row is not None
    if derive_state(row, await _events(session, run)) in TERMINAL:
        return await execution_dict(session, row, run)
    now = _now()
    actor = f"principal:{authority.actor_id}"
    if row.cancel_requested_at is None:
        row.cancel_requested_at = now
        _event(session, run, "cancel_requested", "cancel_requested", actor, {})
    if row.started_at is None and row.released_at is None:
        row.released_at = now
        _event(session, run, "cancelled", "0:cancelled", actor,
               {"reason": "Cancelled before it started", "in_flight": None})
    await session.flush()
    return await execution_dict(session, row, run)


async def execution_state(factory: SessionFactory, run_id: uuid.UUID) -> dict:
    async with factory() as session:
        row = await fresh(session, AgentRunExecution, AgentRunExecution.run_id == run_id)
        run = await fresh(session, AgentRun, AgentRun.id == run_id)
        if row is None or run is None:
            return {"run_id": str(run_id), "state": "missing"}
        return await execution_dict(session, row, run)
