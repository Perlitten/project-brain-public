"""Integration queue: completed runs re-validated and marked integrated/rejected."""
from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from brain.auth.principals import ResolvedPrincipal
from brain.database.harness_models import AgentTask, AgentTaskEvent
from brain.database.models import Repository
from brain.config.settings import settings
from brain.team import combined_patch
from brain.team._util import _iso, _now
from brain.team.activity import record_event
from brain.team.authorization import Permission, TeamConflict, TeamDenied, authorize, fresh
from brain.team.models import AgentRun, ProjectRepository, TeamIntegrationEntry, TeamTaskBinding
from brain.team.provisioning import TeamNotFound
from brain.team.coordination._common import FINAL, INACTIVE_TASK_STATUSES, PENDING, _uuid
from brain.team.coordination.instructions import superseded_instructions


def entry_dict(entry: TeamIntegrationEntry, run: AgentRun | None = None) -> dict:
    return {"id": entry.id, "project_id": str(entry.project_id), "repository_id": entry.repository_id,
            "task_id": str(entry.task_id), "run_id": str(entry.run_id), "base_sha": entry.base_sha,
            "head_sha": entry.head_sha, "status": entry.status, "reason": entry.reason,
            "checked_base_sha": entry.checked_base_sha, "checked_at": _iso(entry.checked_at),
            "enqueued_by": entry.enqueued_by, "enqueued_at": _iso(entry.enqueued_at),
            "decided_by": entry.decided_by, "decided_at": _iso(entry.decided_at), "version": entry.version,
            "initiator_id": run.initiator_id if run is not None else None,
            "requested_by": run.requested_by if run is not None else None}


async def _executed_base(session: AsyncSession, run: AgentRun) -> str:
    """The base the executor actually checked out (``run.execution.completed``), else the declared base."""
    event = (await session.execute(select(AgentTaskEvent).where(
        AgentTaskEvent.task_id == run.task_id, AgentTaskEvent.event_type == "run.execution.completed",
        AgentTaskEvent.idempotency_key.like(f"exec:{run.id}:%")).order_by(AgentTaskEvent.id.desc())
        .limit(1))).scalar_one_or_none()
    base = (event.payload_json or {}).get("base_sha") if event is not None else None
    return base if isinstance(base, str) and re.fullmatch(r"[0-9a-f]{40}", base) else run.base_sha


async def _coordination_event(session: AsyncSession, kind: str, summary: str, entry: TeamIntegrationEntry,
                              actor_id: int, **details) -> None:
    await record_event(session, kind=kind, summary=summary, audience="repository", actor_principal_id=actor_id,
                       project_id=entry.project_id, repository_id=entry.repository_id,
                       details={"entry_id": entry.id, "task_id": str(entry.task_id), "run_id": str(entry.run_id),
                                **details})


async def enqueue(session: AsyncSession, caller: ResolvedPrincipal, project_id: uuid.UUID, payload: dict) -> dict:
    """Queue a completed run (one with a recorded head revision) for integration; idempotent per run."""
    authority = await authorize(session, caller, project_id, Permission.REQUEST_RUN)
    run = await fresh(session, AgentRun, AgentRun.id == _uuid(payload["run_id"], "run id"))
    binding = await fresh(session, TeamTaskBinding, TeamTaskBinding.task_id == run.task_id,
                          TeamTaskBinding.project_id == project_id) if run is not None else None
    if run is None or binding is None:
        raise TeamNotFound("Run not found")
    await authorize(session, caller, project_id, Permission.READ, task_id=run.task_id)
    existing = await fresh(session, TeamIntegrationEntry, TeamIntegrationEntry.run_id == run.id)
    if existing is not None:
        return entry_dict(existing, run)
    if run.revoked_at is not None:
        raise TeamConflict("Run was revoked")
    if not run.head_sha:
        raise TeamConflict("Run has no recorded head revision; only completed runs can be queued")
    try:
        async with session.begin_nested():
            entry = TeamIntegrationEntry(project_id=project_id, repository_id=binding.repository_id,
                                         task_id=run.task_id, run_id=run.id, base_sha=await _executed_base(session, run),
                                         head_sha=run.head_sha, status="queued", enqueued_by=authority.actor_id,
                                         version=1)
            session.add(entry)
            await session.flush()
    except IntegrityError:
        found = await fresh(session, TeamIntegrationEntry, TeamIntegrationEntry.run_id == run.id)
        if found is None:
            raise
        return entry_dict(found, run)
    await _coordination_event(session, "coordination.integration_queued", "Run queued for integration", entry,
                              authority.actor_id, head_sha=entry.head_sha)
    return entry_dict(entry, run)


async def _settle_task(session: AsyncSession, entry: TeamIntegrationEntry, actor_id: int) -> None:
    """Mirror a final queue outcome on the task: integrated completes it, rejected is recorded only.

    ``team_task_bindings`` has no status of its own; the task's
    ``agent_tasks.status`` is the one that ``_done``, ``_active_plans`` and the
    run-request gate read."""
    task = await fresh(session, AgentTask, AgentTask.id == entry.task_id, exclusive=True)
    if task is None:
        return
    payload = {"entry_id": entry.id, "run_id": str(entry.run_id), "head_sha": entry.head_sha,
               "reason": entry.reason}
    actor = f"principal:{actor_id}"
    if entry.status == "integrated" and task.status not in INACTIVE_TASK_STATUSES:
        previous = task.status
        changed = await session.execute(update(AgentTask).where(
            AgentTask.id == task.id, AgentTask.version == task.version)
            .values(status="completed", version=task.version + 1).returning(AgentTask.version))
        version = changed.scalar_one_or_none()
        if version is None:
            raise TeamConflict("Task changed concurrently; retry the integration")
        session.add(AgentTaskEvent(task_id=task.id, event_type="status_change", actor=actor,
                                   classification="decision", expected_task_version=version - 1,
                                   task_version=version, idempotency_key=f"integration:{entry.id}:completed",
                                   payload_json={"from": previous, "to": "completed",
                                                 "note": "Run integrated from the team integration queue",
                                                 **payload}))
    elif entry.status in FINAL:
        session.add(AgentTaskEvent(task_id=task.id, event_type=f"integration.{entry.status}", actor=actor,
                                   task_version=task.version, idempotency_key=f"integration:{entry.id}:{entry.status}",
                                   payload_json=payload))
    await session.flush()


async def _entry(session: AsyncSession, project_id: uuid.UUID, entry_id: int, expected_version: int
                 ) -> TeamIntegrationEntry:
    entry = await fresh(session, TeamIntegrationEntry, TeamIntegrationEntry.id == entry_id,
                        TeamIntegrationEntry.project_id == project_id, exclusive=True)
    if entry is None:
        raise TeamNotFound("Queue entry not found")
    if entry.version != expected_version:
        raise TeamConflict("Queue entry version conflict")
    if entry.status in FINAL:
        raise TeamConflict(f"Queue entry is already {entry.status}")
    return entry


def _run_copy(path: str | None) -> str | None:
    """A run's working copy, only when it lies inside the operator's execution root."""
    root = settings.BRAIN_EXECUTION_ROOT
    if not path or not root:
        return None
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
    except (OSError, ValueError):
        return None
    return path


async def _combined_patch(session: AsyncSession, entry: TeamIntegrationEntry, run: AgentRun,
                          integrated: list[TeamIntegrationEntry]) -> dict:
    """``entry``'s patch applied after ``integrated`` (oldest first) on its base, off the event loop."""
    repository = await fresh(session, Repository, Repository.id == entry.repository_id)
    runs = {row.id: row for row in (await session.execute(select(AgentRun).where(
        AgentRun.id.in_([item.run_id for item in integrated])))).scalars().all()} if integrated else {}
    sources = [repository.path if repository is not None else None, _run_copy(run.worktree_path),
               *(_run_copy(runs[item.run_id].worktree_path) for item in integrated if item.run_id in runs)]
    return await asyncio.to_thread(
        combined_patch.combine, entry.base_sha,
        [combined_patch.Patch(str(item.id), item.head_sha) for item in integrated],
        combined_patch.Patch(str(entry.id), entry.head_sha), sources)


async def integrate(session: AsyncSession, caller: ResolvedPrincipal, project_id: uuid.UUID, entry_id: int,
                    payload: dict) -> dict:
    """Re-validate one entry against the current base and mark it integrated, or record why not.

    Requires task acceptance (Reviewer) by someone other than the run's
    initiator or requester. Entries are taken in queue order: an earlier entry
    still ``queued`` in the same repository must be integrated or rejected first.
    The outcome is returned (and committed) rather than raised, so a
    ``needs_rebase`` or ``stale_instructions`` verdict stays on record.

    A run built on an older base is not sent back for a rebase when the
    current base is the head of an entry integrated on that same base: its
    patch is applied after those entries' patches in a temporary index
    (``combined_patch``). Clean -> checked like any other entry; conflict ->
    ``rejected`` with the paths; the check could not run -> ``needs_rebase``.
    Nothing is merged or pushed; the response carries the combined patch."""
    authority = await authorize(session, caller, project_id, Permission.ACCEPT)
    entry = await _entry(session, project_id, entry_id, payload["expected_version"])
    run = await fresh(session, AgentRun, AgentRun.id == entry.run_id)
    if authority.initiator_id in {run.initiator_id, run.requested_by}:
        raise TeamDenied("Independent integration is required")
    await fresh(session, ProjectRepository, ProjectRepository.repository_id == entry.repository_id, exclusive=True)
    earlier = (await session.execute(select(TeamIntegrationEntry.id).where(
        TeamIntegrationEntry.repository_id == entry.repository_id, TeamIntegrationEntry.id < entry.id,
        TeamIntegrationEntry.status == "queued").limit(1))).scalar_one_or_none()
    if earlier is not None:
        raise TeamConflict(f"Earlier queue entry {earlier} must be integrated or rejected first")
    integrated = (await session.execute(select(TeamIntegrationEntry).where(
        TeamIntegrationEntry.repository_id == entry.repository_id, TeamIntegrationEntry.status == "integrated")
        .order_by(TeamIntegrationEntry.decided_at.desc(), TeamIntegrationEntry.id.desc()))).scalars().all()
    current_base = payload.get("current_base_sha") or (integrated[0].head_sha if integrated else None)
    if current_base is None:
        raise TeamConflict("current_base_sha is required until an entry has been integrated in this repository")
    # An entry checked against another entry's head (its own base or a combined one) moved the target past it.
    if any(current_base in {item.base_sha, item.checked_base_sha} and item.head_sha != current_base
           for item in integrated):
        raise TeamConflict("current_base_sha predates an integrated entry; use the current target revision")
    now = _now()
    stale: list[dict] = []
    combined: dict | None = None
    status: str | None = None
    reason: str | None = None
    # Heads of entries integrated by combination: no commit holds the combined result.
    combined_heads = {item.head_sha for item in integrated
                      if item.checked_base_sha is not None and item.checked_base_sha != item.base_sha}
    if run.revoked_at is not None:
        status, reason = "rejected", "Run was revoked"
    elif entry.base_sha == current_base and current_base in combined_heads:
        status, reason = "needs_rebase", (f"Run base {current_base[:12]} is the head of a combined integration, not "
                                          "the merged result; pass current_base_sha of the merged revision")
    elif entry.base_sha != current_base:
        siblings = [item for item in reversed(integrated) if item.base_sha == entry.base_sha]  # oldest first
        rebase = f"Run base {entry.base_sha[:12]} is not the current base {current_base[:12]}"
        if current_base not in {item.head_sha for item in siblings}:
            status, reason = "needs_rebase", rebase
        else:
            combined = await _combined_patch(session, entry, run, siblings)
            if combined["status"] == "conflict":
                status = "rejected"
                reason = (f"Patch conflicts with integrated tasks on base {entry.base_sha[:12]}: "
                          + ", ".join(combined["conflicts"]))
            elif combined["status"] != "clean":
                status, reason = "needs_rebase", f"{rebase}; combined patch check unavailable: {combined['reason']}"
    else:
        combined = await _combined_patch(session, entry, run, [])  # the patch alone, for the integrator
    if status is None:
        stale = await superseded_instructions(session, entry.run_id) or []
        if stale:
            status = "stale_instructions"
            reason = "Instructions changed since the run started: " + ", ".join(
                f"{item['kind']} {item['id']} ({item['reason']})" for item in stale[:5])
        else:
            status, reason = "integrated", None
    values: dict = {"status": status, "reason": reason and reason[:255], "checked_base_sha": current_base,
                    "checked_at": now, "version": entry.version + 1}
    if status in FINAL:
        values.update(decided_by=authority.actor_id, decided_at=now)
    changed = await session.execute(update(TeamIntegrationEntry).where(
        TeamIntegrationEntry.id == entry.id, TeamIntegrationEntry.version == entry.version)
        .values(**values).returning(TeamIntegrationEntry.id))
    if changed.scalar_one_or_none() is None:
        raise TeamConflict("Queue entry version conflict")
    entry = await fresh(session, TeamIntegrationEntry, TeamIntegrationEntry.id == entry_id)
    kinds = {"integrated": ("coordination.integrated", "Run integrated"),
             "needs_rebase": ("coordination.needs_rebase", "Run needs a rebase onto the current base"),
             "stale_instructions": ("coordination.stale_instructions", "Run resolved instructions that were superseded"),
             "rejected": ("coordination.integration_rejected", "Run rejected from the integration queue")}
    kind, summary = kinds[status]
    if status in FINAL:
        await _settle_task(session, entry, authority.actor_id)
    await _coordination_event(session, kind, summary, entry, authority.actor_id, base_sha=current_base,
                              superseded=stale[:20],
                              combined_patch=combined_patch.summary(combined) if combined is not None else None)
    return {**entry_dict(entry, run), "superseded_instructions": stale, "combined_patch": combined}


async def reject(session: AsyncSession, caller: ResolvedPrincipal, project_id: uuid.UUID, entry_id: int,
                 payload: dict) -> dict:
    """Take an entry out of the queue: a Reviewer, or the member who queued it (withdrawal)."""
    try:
        authority = await authorize(session, caller, project_id, Permission.ACCEPT)
        reviewer = True
    except TeamDenied:
        authority = await authorize(session, caller, project_id, Permission.READ)
        reviewer = False
    entry = await _entry(session, project_id, entry_id, payload["expected_version"])
    if not reviewer and authority.actor_id != entry.enqueued_by:
        raise TeamDenied("Only a reviewer or the member who queued it can remove a queue entry")
    now = _now()
    changed = await session.execute(update(TeamIntegrationEntry).where(
        TeamIntegrationEntry.id == entry.id, TeamIntegrationEntry.version == entry.version)
        .values(status="rejected", reason=str(payload["reason"])[:255], decided_by=authority.actor_id,
                decided_at=now, version=entry.version + 1).returning(TeamIntegrationEntry.id))
    if changed.scalar_one_or_none() is None:
        raise TeamConflict("Queue entry version conflict")
    entry = await fresh(session, TeamIntegrationEntry, TeamIntegrationEntry.id == entry_id)
    await _settle_task(session, entry, authority.actor_id)
    await _coordination_event(session, "coordination.integration_rejected", "Run removed from the integration queue",
                              entry, authority.actor_id, reason=entry.reason)
    return entry_dict(entry)


async def queue_view(session: AsyncSession, project_id: uuid.UUID, repository_id: int | None = None,
                     include_done: bool = False) -> list[dict]:
    query = select(TeamIntegrationEntry).where(TeamIntegrationEntry.project_id == project_id)
    if repository_id is not None:
        query = query.where(TeamIntegrationEntry.repository_id == repository_id)
    if not include_done:
        query = query.where(TeamIntegrationEntry.status.in_(PENDING))
    entries = (await session.execute(query.order_by(TeamIntegrationEntry.id).limit(500))).scalars().all()
    run_ids = [entry.run_id for entry in entries]
    runs = ({row.id: row for row in (await session.execute(select(AgentRun).where(AgentRun.id.in_(run_ids))))
             .scalars().all()} if run_ids else {})
    return [entry_dict(entry, runs.get(entry.run_id)) for entry in entries]


async def list_queue(session: AsyncSession, caller: ResolvedPrincipal, project_id: uuid.UUID,
                     repository_id: int | None = None, include_done: bool = False) -> list[dict]:
    await authorize(session, caller, project_id, Permission.READ)
    return await queue_view(session, project_id, repository_id, include_done)
