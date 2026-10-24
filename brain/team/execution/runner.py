"""The bounded runner: checkpoints authorization and fencing before every effect."""
from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from brain.auth.principals import ResolvedPrincipal
from brain.database.models import Repository
from brain.execution.bounded import ExecutorBackend, ExecutorError, StepRequest
from brain.team._util import _now
from brain.team.authorization import TeamConflict, TeamDenied, fresh
from brain.team.models import AgentRun, AgentRunExecution, TeamTaskBinding
from brain.team.execution._common import SessionFactory, _event
from brain.team.execution.artifacts import ArtifactDraft, _patch_artifact, _store_artifacts, step_artifacts
from brain.team.execution.lease import Lease, _fence, acquire_lease, recover_orphaned_runs, worker_owner
from brain.team.execution.policy import (Capabilities, ExecutionPolicy, authorize_execution, default_backend, operator_policy)
from brain.team.execution.state import execution_state


@dataclass
class ExecutionRunner:
    """One owner's handle on one execution: checkpoints, evidence writes and the terminal record."""
    factory: SessionFactory
    lease: Lease
    project_id: uuid.UUID
    task_id: uuid.UUID
    policy: ExecutionPolicy
    backend: ExecutorBackend
    stopped: tuple[str, str] | None = None

    @property
    def actor(self) -> str:
        return f"run:{self.lease.run_id}"

    async def _deny_reason(self, session: AsyncSession, row: AgentRunExecution, *,
                           starting: bool = False) -> str | None:
        if row.backend != self.backend.name:
            return "The executor backend changed since the request"
        requester = ResolvedPrincipal(row.requested_by, "", "", credential_id=row.requested_credential_id)
        try:
            await authorize_execution(session, requester, self.project_id, self.task_id, self.lease.run_id,
                                      policy=self.policy, backend=self.backend,
                                      capabilities=Capabilities.from_row(row))
        except TeamDenied as exc:
            return str(exc)
        if starting:
            from brain.team import coordination  # lazy: see request_execution

            try:
                await coordination.require_dependencies_done(session, self.task_id)
            except TeamConflict as exc:
                return str(exc)
        return None

    async def checkpoint(self, write: Callable[[AsyncSession, AgentRunExecution, AgentRun], Awaitable[None]]
                         | None = None, *, starting: bool = False) -> tuple[str, str]:
        """Fence, re-authorize, check cancellation and budget, then run ``write`` in the same transaction.

        ``starting`` also re-checks the task's M3 dependencies (the checkpoint
        before the first effect). Returns ``("ok", "")`` or ``(outcome, reason)``.
        On revocation the cancellation request is committed even though nothing
        else is."""
        async with self.factory() as session, session.begin():
            row = await _fence(session, self.lease, self.policy.lease_seconds)
            if row is None:
                return "fenced", "The lease was lost; this owner may not commit results"
            if row.cancel_requested_at is not None:
                return "cancelled", "Cancellation was requested"
            reason = await self._deny_reason(session, row, starting=starting)
            if reason is not None:
                row.cancel_requested_at = _now()
                return "revoked", reason
            started = row.started_at or _now()
            # The wall budget gates new effects; recording what already happened stays allowed.
            if write is None and (_now() - started).total_seconds() >= int(row.budget["wall_seconds"]):
                return "budget_exhausted", "The wall-time budget is exhausted"
            if write is not None:
                run = await fresh(session, AgentRun, AgentRun.id == self.lease.run_id)
                assert run is not None
                await write(session, row, run)
            return "ok", ""

    async def probe(self) -> str | None:
        """Called while a step runs: renews the lease and stops the step on any failed check."""
        outcome, reason = await self.checkpoint()
        if outcome == "ok":
            return None
        self.stopped = (outcome, reason)
        return outcome

    async def conclude(self, outcome: str, payload: dict) -> bool:
        """Write the terminal event and release the lease; a fenced owner writes nothing and gets ``False``."""
        async with self.factory() as session, session.begin():
            row = await _fence(session, self.lease, self.policy.lease_seconds)
            if row is None:
                return False
            run = await fresh(session, AgentRun, AgentRun.id == self.lease.run_id)
            assert run is not None
            now = _now()
            if outcome in ("revoked", "cancelled") and row.cancel_requested_at is None:
                row.cancel_requested_at = now
            row.released_at = now
            _event(session, run, outcome, f"{self.lease.token}:{outcome}", self.actor,
                   {"fencing_token": self.lease.token, **payload})
            return True


async def _changed_paths(backend: ExecutorBackend, copy: Any) -> list[str] | None:
    """Files the run changed against its base (committed, uncommitted and untracked); ``None`` if unreadable.

    Overlap detection is advisory, so a backend that cannot list changes does
    not fail an otherwise completed run."""
    lister = getattr(backend, "changed_paths", None)
    if lister is None:
        return None
    try:
        return list(await asyncio.to_thread(lister, copy))
    except ExecutorError as exc:
        logger.warning("Could not list changed files for run {}: {}", copy.run_dir.name, exc)
        return None


def _failure(outcome_evidence: dict) -> str:
    if outcome_evidence.get("error"):
        return str(outcome_evidence["error"])
    if outcome_evidence.get("timed_out"):
        return "The step exceeded its timeout"
    return f"The step exited with code {outcome_evidence.get('exit_code')}"


async def execute_run(run_id: uuid.UUID, *, owner: str | None = None, factory: SessionFactory | None = None,
                      policy: ExecutionPolicy | None = None, backend: ExecutorBackend | None = None) -> dict:
    """Run a requested execution to a terminal event (worker entry point). Never raises on a denial."""
    if factory is None:
        from brain.database.session import async_session_factory
        factory = async_session_factory
    policy = policy or operator_policy()
    backend = backend or default_backend(policy)
    owner = owner or worker_owner()
    await recover_orphaned_runs(factory, reaper=getattr(backend, "reap", None))
    async with factory() as session:
        row = await fresh(session, AgentRunExecution, AgentRunExecution.run_id == run_id)
        run = await fresh(session, AgentRun, AgentRun.id == run_id)
        binding = await fresh(session, TeamTaskBinding, TeamTaskBinding.task_id == run.task_id) if run else None
        repository = (await fresh(session, Repository, Repository.id == binding.repository_id)) if binding else None
        if row is None or run is None or binding is None or repository is None:
            return {"run_id": str(run_id), "state": "missing"}
        steps = list(row.steps)
        environment_name, egress = row.environment, tuple(row.egress)
        source, base_sha, branch = Path(repository.path), run.base_sha, run.branch
        project_id, task_id = binding.project_id, run.task_id
    lease = await acquire_lease(factory, run_id, owner, lease_seconds=policy.lease_seconds)
    if lease is None:
        return await execution_state(factory, run_id)
    runner = ExecutionRunner(factory, lease, project_id, task_id, policy, backend)

    async def stop(outcome: str, reason: str, in_flight: dict | None, recorded: int) -> dict:
        if outcome != "fenced":
            await runner.conclude(outcome, {"reason": reason, "steps_recorded": recorded, "in_flight": in_flight})
        return await execution_state(factory, run_id)

    # Every gate (including a known environment for this backend and the task's dependencies) before the first
    # docker call.
    outcome, reason = await runner.checkpoint(starting=True)
    if outcome != "ok":
        return await stop(outcome, reason, None, 0)
    try:
        resolved = await asyncio.to_thread(backend.resolve_environment,
                                           policy.environments.get(environment_name) if environment_name else None)
    except ExecutorError as exc:
        await runner.conclude("failed", {"reason": str(exc), "phase": "environment", "steps_recorded": 0,
                                         "in_flight": None})
        return await execution_state(factory, run_id)
    environment_evidence = resolved.evidence() if hasattr(resolved, "evidence") else None

    async def started(session: AsyncSession, _row: AgentRunExecution, current: AgentRun) -> None:
        _event(session, current, "started", f"{lease.token}:started", runner.actor,
               {"owner": lease.owner, "fencing_token": lease.token, "backend": backend.name, "base_sha": base_sha,
                "branch": branch, "environment": environment_evidence,
                "egress": {"profile": "allowlist" if egress else "none", "hosts": list(egress)}})

    outcome, reason = await runner.checkpoint(started)
    if outcome != "ok":
        return await stop(outcome, reason, None, 0)
    try:
        assert policy.root is not None
        copy = await asyncio.to_thread(backend.prepare, policy.root, str(run_id), source, base_sha, branch)
    except ExecutorError as exc:
        await runner.conclude("failed", {"reason": str(exc), "phase": "prepare", "steps_recorded": 0,
                                         "in_flight": None})
        return await execution_state(factory, run_id)
    used_secrets: set[str] = set()
    try:
        for index, step in enumerate(steps):
            outcome, reason = await runner.checkpoint()
            if outcome != "ok":
                return await stop(outcome, reason, None, index)
            tool = policy.tools.get(step["tool"])
            if tool is None:
                return await stop("revoked", "Tool is no longer allowlisted by the operator", None, index)
            missing = [ref for ref in step["secrets"]
                       if ref not in policy.secrets or not os.environ.get(policy.secrets[ref])]
            if missing:
                await runner.conclude("failed", {"reason": "A secret reference is not configured on the server",
                                                 "failed_step": index, "steps_recorded": index, "in_flight": None})
                return await execution_state(factory, run_id)
            secrets = {ref: os.environ[policy.secrets[ref]] for ref in step["secrets"]}
            used_secrets.update(secrets.values())
            async with factory() as session:
                current = await fresh(session, AgentRunExecution, AgentRunExecution.run_id == run_id)
                assert current is not None and current.started_at is not None
                budget = dict(current.budget)
                elapsed = (_now() - current.started_at).total_seconds()
            remaining = max(1.0, float(budget["wall_seconds"]) - elapsed)
            request = StepRequest(index=index, tool=tool, args=tuple(step["args"]), secrets=secrets,
                                  timeout_seconds=min(float(budget["step_timeout_seconds"]), remaining),
                                  output_cap=int(budget["output_bytes"]),
                                  check_interval=max(0.2, min(5.0, policy.lease_seconds / 3)),
                                  environment=resolved, egress=egress)
            result = await backend.run_step(copy, request, runner.probe)
            evidence = result.evidence()
            if result.stopped:
                stopped_outcome, stopped_reason = runner.stopped or (result.stopped, "Stopped")
                return await stop(stopped_outcome, stopped_reason,
                                  {"step": index, "tool": tool.name, "state": "terminated", "evidence": evidence,
                                   "note": "The process was stopped; changes it made in the working copy before "
                                           "that are not undone, and network effects cannot be recalled."}, index)

            drafts = await asyncio.to_thread(step_artifacts, copy, tool, index, result.log_path)

            async def record(session: AsyncSession, current_row: AgentRunExecution, current: AgentRun,
                             evidence: dict = evidence, index: int = index,
                             drafts: list[ArtifactDraft] = drafts) -> None:
                current_row.steps_done = index + 1
                _event(session, current, "step", f"{lease.token}:step:{index}", runner.actor, evidence)
                await _store_artifacts(session, current_row, lease.token, drafts, used_secrets)

            outcome, reason = await runner.checkpoint(record)
            if outcome != "ok":
                return await stop(outcome, reason, {"step": index, "tool": tool.name, "state": "finished_unrecorded",
                                                    "evidence": evidence}, index)
            if result.exit_code != 0 or result.timed_out or result.error:
                await runner.conclude("failed", {"reason": _failure(evidence), "failed_step": index,
                                                 "steps_recorded": index + 1, "in_flight": None})
                return await execution_state(factory, run_id)
        head_sha, dirty = await asyncio.to_thread(backend.head, copy)
        changed_paths = await _changed_paths(backend, copy)
        patch = await _patch_artifact(backend, copy)

        async def complete(session: AsyncSession, current_row: AgentRunExecution, current: AgentRun) -> None:
            from brain.team import coordination  # lazy: see request_execution

            now = _now()
            current.head_sha, current.worktree_path = head_sha, str(copy.path)
            if patch is not None:
                await _store_artifacts(session, current_row, lease.token, [patch], used_secrets)
            current_row.released_at = now
            overlaps = await coordination.record_change_overlaps(session, current.task_id, changed_paths,
                                                                 actor_id=current.requested_by)
            _event(session, current, "completed", f"{lease.token}:completed", runner.actor,
                   {"fencing_token": lease.token, "base_sha": base_sha, "head_sha": head_sha, "dirty": dirty,
                    "worktree_path": str(copy.path), "steps_recorded": len(steps),
                    "changed_paths": coordination.changed_scope(changed_paths) if changed_paths is not None else None,
                    "changed_paths_total": len(changed_paths) if changed_paths is not None else None,
                    "overlaps": len(overlaps)})

        outcome, reason = await runner.checkpoint(complete)
        if outcome != "ok":
            return await stop(outcome, reason, {"state": "finished_unrecorded", "head_sha": head_sha}, len(steps))
    except ExecutorError as exc:
        await runner.conclude("failed", {"reason": str(exc), "in_flight": None})
    except Exception as exc:  # noqa: BLE001 - recorded, then re-raised for the worker's own failure handling
        await runner.conclude("failed", {"reason": f"Internal executor error: {type(exc).__name__}",
                                         "in_flight": None})
        raise
    finally:
        await asyncio.to_thread(backend.release, copy)
    return await execution_state(factory, run_id)
