"""Team coordination (M3; ADR docs/adr/team-collaboration.md, "Revocation, concurrency and approval").

Five pieces, all on top of the M1 task/run model and the central ``authorize()``:

* **Subtasks and a coordinator.** A subtask is an ordinary team task whose
  ``team_task_plans`` row names its parent. ``depends_on`` lists sibling
  subtasks; the dependency graph is kept acyclic on every write, and
  ``brain.team.store.request_run`` refuses a run while a dependency is not done
  (``require_dependencies_done``). The bounded executor checks again when the
  execution is requested and when it actually starts (``brain.team.execution``). ``plan_view`` derives each subtask's state
  and the ready set in topological order; ``dispatch`` requests runs for it.
* **Overlap detection.** Declared scope paths of active tasks in one
  repository, and live path leases, are compared by path prefix (a glob counts
  from its static prefix). An overlap is a warning in the response and a
  ``coordination.overlap`` row in the activity feed. When a bounded execution
  completes, the files it actually changed are compared the same way
  (``record_change_overlaps``). It flags risk only: same files can be
  independent and separate files can still conflict.
* **Instruction resolution.** Each new run records the canonical, active
  decision and rule versions that applied to its repository
  (``team_run_resolutions``). Integration flags a run whose instructions were
  edited, superseded or retired since.
* **Critical leases.** Exclusive, expiring leases on repository paths or named
  resources with fencing tokens (``team_resource_leases``), the same
  compare-and-swap pattern as the M2 execution lease. Revoking a member
  releases their leases (``brain.team.provisioning.revoke_principal_access``);
  a project administrator can force-release a lease with a recorded reason
  (``force_release_lease``), which also bumps the fencing token.
* **Integration queue.** Completed runs (a recorded head revision) are queued
  per repository. ``integrate`` re-validates the next entry against the
  current base revision: a different base gives ``needs_rebase``, superseded
  instructions give ``stale_instructions``, otherwise an independent reviewer
  marks it ``integrated``. No git merge happens here. An integrated entry
  completes its task (``agent_tasks.status = "completed"`` plus a
  ``status_change`` event); a rejected one is recorded on the task ledger and
  leaves the task open for another run.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from brain.auth.principals import ResolvedPrincipal
from brain.database.harness_models import AgentTask, AgentTaskEvent
from brain.database.models import Decision, Repository, Rule
from brain.database.repository_utils import repository_path_candidates
from brain.team.activity import record_event
from brain.team.authorization import (Permission, TeamConflict, TeamDenied, administrator, authorize, fresh,
                                      permissions)
from brain.team.models import (AgentRun, ProjectRepository, TeamActivityEvent, TeamIntegrationEntry,
                               TeamResourceLease, TeamRunResolution, TeamTaskBinding, TeamTaskPlan)
from brain.team.provisioning import TeamNotFound
from brain.team.store import create_task, digest, request_run, task_dict

MAX_SCOPE_PATHS = 64
MAX_DEPENDENCIES = 32
MAX_SUBTASKS = 200
MAX_RESOLVED = 500
MIN_LEASE_SECONDS = 10
MAX_LEASE_SECONDS = 3600
# Changed files compared for overlap after an execution, and path pairs kept per warning.
MAX_CHANGED_PATHS = 5000
MAX_OVERLAP_PAIRS = 50
MAX_REASON = 500
INACTIVE_TASK_STATUSES = ("completed", "cancelled")
PENDING = ("queued", "needs_rebase", "stale_instructions")
FINAL = ("integrated", "rejected")
_GLOB = re.compile(r"[*?\[]")
_DRIVE = re.compile(r"^[A-Za-z]:")
_RESOURCE = re.compile(r"^resource:[a-z0-9][a-z0-9._-]{0,127}$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _uuid(value: object, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError as exc:
        raise TeamConflict(f"Invalid {what}: {value!r}") from exc


# --------------------------------------------------------------------------- paths and overlap


def normalize_path(value: str, *, allow_glob: bool = True) -> str:
    """A repository-relative POSIX path; absolute paths, ``..`` and empty paths are refused."""
    raw = str(value).strip().replace("\\", "/")
    if not raw or len(raw) > 512 or raw.startswith("/") or _DRIVE.match(raw) or "\x00" in raw:
        raise TeamConflict(f"Scope path must be repository-relative: {value!r}")
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if not parts or ".." in parts:
        raise TeamConflict(f"Scope path must stay inside the repository: {value!r}")
    path = "/".join(parts)
    if not allow_glob and _GLOB.search(path):
        raise TeamConflict(f"Lease paths cannot be globs: {value!r}")
    return path


def normalize_scope(values: list[str]) -> list[str]:
    if len(values) > MAX_SCOPE_PATHS:
        raise TeamConflict(f"At most {MAX_SCOPE_PATHS} scope paths are allowed")
    return sorted({normalize_path(value) for value in values})


def normalize_changed_path(value: str) -> str:
    """Validate a literal repository-relative path reported by Git without rewriting its filename."""
    raw = str(value)
    if not raw or len(raw) > 4096 or raw.startswith("/") or _DRIVE.match(raw) or "\x00" in raw:
        raise TeamConflict(f"Changed path must be repository-relative: {value!r}")
    if any(part in ("", ".", "..") for part in raw.split("/")):
        raise TeamConflict(f"Changed path must stay inside the repository: {value!r}")
    return raw


def normalize_resource(value: str) -> str:
    """``resource:<name>`` for a named resource, otherwise a repository-relative path without globs."""
    raw = str(value).strip()
    if raw.startswith("resource:"):
        if not _RESOURCE.match(raw):
            raise TeamConflict("Resource names are lowercase letters, digits, '.', '_' or '-' after 'resource:'")
        return raw
    return normalize_path(raw, allow_glob=False)


def _static_prefix(path: str) -> str:
    match = _GLOB.search(path)
    if match is None:
        return path
    head = path[:match.start()]
    return head.rsplit("/", 1)[0] if "/" in head else ""


def paths_overlap(first: str, second: str) -> bool:
    """Whether two scope entries may touch the same files: equal, nested, or a glob reaching the other."""
    if first.startswith("resource:") or second.startswith("resource:"):
        return first == second
    a, b = _static_prefix(first), _static_prefix(second)
    if not a or not b:
        return True
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def overlapping(mine: list[str], theirs: list[str]) -> list[list[str]]:
    return [[a, b] for a in mine for b in theirs if paths_overlap(a, b)]


# --------------------------------------------------------------------------- plans, dependencies, coordinator


def plan_dict(plan: TeamTaskPlan | None, task_id: uuid.UUID) -> dict:
    if plan is None:
        return {"task_id": str(task_id), "parent_task_id": None, "depends_on": [], "scope_paths": [], "version": 0}
    return {"task_id": str(plan.task_id), "parent_task_id": str(plan.parent_task_id) if plan.parent_task_id else None,
            "depends_on": list(plan.depends_on or []), "scope_paths": list(plan.scope_paths or []),
            "version": plan.version}


async def _binding(session: AsyncSession, project_id: uuid.UUID, task_id: uuid.UUID) -> TeamTaskBinding:
    binding = await fresh(session, TeamTaskBinding, TeamTaskBinding.task_id == task_id,
                          TeamTaskBinding.project_id == project_id)
    if binding is None:
        raise TeamDenied("Task access denied")
    return binding


async def _siblings(session: AsyncSession, parent_id: uuid.UUID) -> list[TeamTaskPlan]:
    return list((await session.execute(select(TeamTaskPlan).where(TeamTaskPlan.parent_task_id == parent_id)
                                       .order_by(TeamTaskPlan.created_at, TeamTaskPlan.task_id)
                                       .limit(MAX_SUBTASKS))).scalars().all())


def find_cycle(graph: dict[str, list[str]]) -> list[str] | None:
    """A dependency cycle as a path (first node repeated at the end), or ``None``."""
    state: dict[str, int] = {}
    for start in sorted(graph):
        if state.get(start):
            continue
        stack: list[tuple[str, int]] = [(start, 0)]
        path: list[str] = []
        while stack:
            node, index = stack.pop()
            if index == 0:
                state[node] = 1
                path.append(node)
            edges = sorted(graph.get(node, []))
            if index < len(edges):
                stack.append((node, index + 1))
                child = edges[index]
                if state.get(child) == 1:
                    return path[path.index(child):] + [child]
                if not state.get(child):
                    stack.append((child, 0))
            else:
                state[node] = 2
                path.pop()
    return None


async def _validate_dependencies(session: AsyncSession, parent_id: uuid.UUID | None, task_id: uuid.UUID | None,
                                 raw: list[str]) -> list[str]:
    if len(raw) > MAX_DEPENDENCIES:
        raise TeamConflict(f"At most {MAX_DEPENDENCIES} dependencies are allowed")
    ids = sorted({str(_uuid(item, "dependency id")) for item in raw})
    if not ids:
        return []
    if parent_id is None:
        raise TeamConflict("Only subtasks of the same parent task can depend on each other")
    if task_id is not None and str(task_id) in ids:
        raise TeamConflict("A task cannot depend on itself")
    siblings = {str(plan.task_id) for plan in await _siblings(session, parent_id)}
    missing = [item for item in ids if item not in siblings]
    if missing:
        raise TeamConflict(f"Dependencies must be subtasks of the same parent task: {', '.join(missing)}")
    return ids


async def _done(session: AsyncSession, task_ids: list[uuid.UUID]) -> set[str]:
    """Tasks that are done: status ``completed``.

    Integrating a run sets that status (``integrate``); an integrated queue
    entry also counts so entries integrated before that existed stay done."""
    if not task_ids:
        return set()
    completed = (await session.execute(select(AgentTask.id).where(AgentTask.id.in_(task_ids),
                                                                  AgentTask.status == "completed"))).scalars().all()
    integrated = (await session.execute(select(TeamIntegrationEntry.task_id).where(
        TeamIntegrationEntry.task_id.in_(task_ids), TeamIntegrationEntry.status == "integrated"))).scalars().all()
    return {str(item) for item in (*completed, *integrated)}


async def require_dependencies_done(session: AsyncSession, task_id: uuid.UUID) -> None:
    """Refuse a run request for a subtask whose dependencies are not done (called by ``store.request_run``)."""
    plan = (await session.execute(select(TeamTaskPlan).where(TeamTaskPlan.task_id == task_id))).scalar_one_or_none()
    if plan is None or not plan.depends_on:
        return
    done = await _done(session, [uuid.UUID(item) for item in plan.depends_on])
    waiting = [item for item in plan.depends_on if item not in done]
    if waiting:
        raise TeamConflict(f"Dependencies are not done: {', '.join(waiting)}")


def _topological(order: list[str], graph: dict[str, list[str]]) -> list[str]:
    """Kahn's order over an acyclic graph, ties broken by creation order."""
    position = {item: index for index, item in enumerate(order)}
    remaining = {item: {dep for dep in graph.get(item, []) if dep in position} for item in order}
    result: list[str] = []
    while remaining:
        ready = sorted((item for item, deps in remaining.items() if not deps), key=position.__getitem__)
        if not ready:  # defensive: writes keep the graph acyclic
            result.extend(sorted(remaining, key=position.__getitem__))
            break
        for item in ready:
            result.append(item)
            del remaining[item]
        for deps in remaining.values():
            deps.difference_update(ready)
    return result


async def plan_view(session: AsyncSession, project_id: uuid.UUID, task_id: uuid.UUID) -> dict:
    """The coordinator's view of one task: its plan, its subtasks with derived states and the ready set."""
    binding = await _binding(session, project_id, task_id)
    task = await fresh(session, AgentTask, AgentTask.id == task_id)
    own = (await session.execute(select(TeamTaskPlan).where(TeamTaskPlan.task_id == task_id))).scalar_one_or_none()
    plans = await _siblings(session, task_id)
    ids = [plan.task_id for plan in plans]
    tasks = {str(row.id): row for row in (await session.execute(select(AgentTask).where(AgentTask.id.in_(ids))))
             .scalars().all()} if ids else {}
    bindings = {str(row.task_id): row for row in (await session.execute(
        select(TeamTaskBinding).where(TeamTaskBinding.task_id.in_(ids)))).scalars().all()} if ids else {}
    runs: dict[str, list[str]] = {}
    if ids:
        for run in (await session.execute(select(AgentRun).where(AgentRun.task_id.in_(ids), AgentRun.revoked_at.is_(None))
                                          .order_by(AgentRun.created_at))).scalars().all():
            runs.setdefault(str(run.task_id), []).append(str(run.id))
    done = await _done(session, ids)
    graph = {str(plan.task_id): list(plan.depends_on or []) for plan in plans}
    order = _topological([str(item) for item in ids], graph)
    states: dict[str, str] = {}
    for item in order:
        sub = tasks[item]
        deps = graph.get(item, [])
        if item in done:
            states[item] = "done"
        elif sub.status == "cancelled" or sub.cancellation_requested_at is not None:
            states[item] = "cancelled"
        elif any(states.get(dep) in {"cancelled", "blocked"} for dep in deps):
            states[item] = "blocked"
        elif runs.get(item):
            states[item] = "running"
        elif all(dep in done for dep in deps):
            states[item] = "ready"
        else:
            states[item] = "waiting"
    plan_by_id = {str(plan.task_id): plan for plan in plans}
    subtasks = [{**task_dict(tasks[item], bindings[item]), "plan": plan_dict(plan_by_id[item], plan_by_id[item].task_id),
                 "state": states[item], "waiting_on": [dep for dep in graph.get(item, []) if dep not in done],
                 "runs": runs.get(item, [])} for item in order]
    return {"task": task_dict(task, binding), "plan": plan_dict(own, task_id), "subtasks": subtasks,
            "ready": [item for item in order if states[item] == "ready"]}


async def get_plan(session: AsyncSession, caller: ResolvedPrincipal, project_id: uuid.UUID,
                   task_id: uuid.UUID) -> dict:
    await authorize(session, caller, project_id, Permission.READ, task_id=task_id)
    return await plan_view(session, project_id, task_id)


async def create_subtask(session: AsyncSession, caller: ResolvedPrincipal, project_id: uuid.UUID,
                         parent_id: uuid.UUID, payload: dict) -> dict:
    """Create a subtask of ``parent_id`` (same repository and base, authority no wider than the parent)."""
    await authorize(session, caller, project_id, Permission.READ, task_id=parent_id)
    parent = await _binding(session, project_id, parent_id)
    scope = normalize_scope(payload.get("scope_paths") or [])
    deps = await _validate_dependencies(session, parent_id, None, payload.get("depends_on") or [])
    requested = permissions(payload.get("permissions") or parent.permissions)
    if not requested <= permissions(parent.permissions):
        raise TeamDenied("Subtask authority exceeds the parent task")
    if len(await _siblings(session, parent_id)) >= MAX_SUBTASKS:
        raise TeamConflict(f"A task can have at most {MAX_SUBTASKS} subtasks")
    created = await create_task(session, caller, project_id, {
        "repository_id": parent.repository_id, "title": payload["title"], "goal": payload["goal"],
        "branch": payload.get("branch") or parent.branch, "base_sha": parent.base_sha,
        "permissions": sorted(requested), "idempotency_key": f"subtask:{parent_id}:{payload['idempotency_key']}",
        # Bound into the idempotency digest: a retry with different structure conflicts.
        "parent_task_id": str(parent_id), "depends_on": deps, "scope_paths": scope})
    task_id = uuid.UUID(created["id"])
    plan = await fresh(session, TeamTaskPlan, TeamTaskPlan.task_id == task_id)
    if plan is None:
        try:
            async with session.begin_nested():
                plan = TeamTaskPlan(task_id=task_id, parent_task_id=parent_id, depends_on=deps, scope_paths=scope,
                                    updated_by=caller.id, version=1)
                session.add(plan)
                await session.flush()
        except IntegrityError:
            plan = await fresh(session, TeamTaskPlan, TeamTaskPlan.task_id == task_id)
            if plan is None:
                raise
    if plan.parent_task_id != parent_id:
        raise TeamConflict("Task already belongs to another parent")
    binding = await _binding(session, project_id, task_id)
    warnings = await detect_overlaps(session, binding, plan, actor_id=caller.id, record=True)
    return {**created, "plan": plan_dict(plan, task_id), "warnings": warnings}


async def update_plan(session: AsyncSession, caller: ResolvedPrincipal, project_id: uuid.UUID,
                      task_id: uuid.UUID, payload: dict) -> dict:
    """Declare scope paths and (for subtasks) sibling dependencies; compare-and-swap on ``expected_version``."""
    await authorize(session, caller, project_id, Permission.CREATE_TASK)
    await authorize(session, caller, project_id, Permission.READ, task_id=task_id)
    binding = await _binding(session, project_id, task_id)
    plan = await fresh(session, TeamTaskPlan, TeamTaskPlan.task_id == task_id, exclusive=True)
    current = plan.version if plan is not None else 0
    if payload["expected_version"] != current:
        raise TeamConflict("Plan version conflict")
    parent_id = plan.parent_task_id if plan is not None else None
    scope = (normalize_scope(payload["scope_paths"]) if payload.get("scope_paths") is not None
             else list(plan.scope_paths or []) if plan is not None else [])
    deps = list(plan.depends_on or []) if plan is not None else []
    if payload.get("depends_on") is not None:
        if parent_id is not None:
            # Serialise dependency edits among siblings so two concurrent edits cannot close a cycle.
            await fresh(session, TeamTaskBinding, TeamTaskBinding.task_id == parent_id, exclusive=True)
        deps = await _validate_dependencies(session, parent_id, task_id, payload["depends_on"])
        if parent_id is not None:
            graph = {str(item.task_id): list(item.depends_on or []) for item in await _siblings(session, parent_id)}
            graph[str(task_id)] = deps
            cycle = find_cycle(graph)
            if cycle:
                raise TeamConflict(f"Dependency cycle: {' -> '.join(cycle)}")
    try:
        async with session.begin_nested():
            if plan is None:
                plan = TeamTaskPlan(task_id=task_id, parent_task_id=None, depends_on=deps, scope_paths=scope,
                                    updated_by=caller.id, version=1)
                session.add(plan)
                await session.flush()
            else:
                changed = await session.execute(update(TeamTaskPlan).where(
                    TeamTaskPlan.task_id == task_id, TeamTaskPlan.version == current)
                    .values(depends_on=deps, scope_paths=scope, updated_by=caller.id, version=current + 1)
                    .returning(TeamTaskPlan.task_id))
                if changed.scalar_one_or_none() is None:
                    raise TeamConflict("Plan version conflict")
                plan = await fresh(session, TeamTaskPlan, TeamTaskPlan.task_id == task_id)
    except IntegrityError as exc:
        raise TeamConflict("Plan version conflict") from exc
    assert plan is not None
    warnings = await detect_overlaps(session, binding, plan, actor_id=caller.id, record=True)
    return {"plan": plan_dict(plan, task_id), "warnings": warnings}


async def dispatch(session: AsyncSession, caller: ResolvedPrincipal, project_id: uuid.UUID,
                   parent_id: uuid.UUID, payload: dict) -> dict:
    """Coordinator step: request one run for every ready subtask, in topological order."""
    await authorize(session, caller, project_id, Permission.REQUEST_RUN)
    await authorize(session, caller, project_id, Permission.READ, task_id=parent_id)
    await fresh(session, TeamTaskBinding, TeamTaskBinding.task_id == parent_id, exclusive=True)
    view = await plan_view(session, project_id, parent_id)
    branches = {item["id"]: item["branch"] for item in view["subtasks"]}
    dispatched = []
    for sub_id in view["ready"]:
        run = await request_run(session, caller, project_id, uuid.UUID(sub_id), {
            "branch": branches[sub_id], "permissions": payload.get("permissions") or ["read"],
            "idempotency_key": f"dispatch:{sub_id}:{payload['idempotency_key']}"})
        dispatched.append(run)
    return {"dispatched": dispatched, "plan": await plan_view(session, project_id, parent_id)}


# --------------------------------------------------------------------------- overlap detection


async def _active_plans(session: AsyncSession, project_id: uuid.UUID,
                        repository_id: int | None = None) -> list[tuple[TeamTaskBinding, TeamTaskPlan, AgentTask]]:
    query = (select(TeamTaskBinding, TeamTaskPlan, AgentTask)
             .join(TeamTaskPlan, TeamTaskPlan.task_id == TeamTaskBinding.task_id)
             .join(AgentTask, AgentTask.id == TeamTaskBinding.task_id)
             .where(TeamTaskBinding.project_id == project_id, AgentTask.status.not_in(INACTIVE_TASK_STATUSES),
                    AgentTask.cancellation_requested_at.is_(None))
             .order_by(TeamTaskBinding.task_id).limit(1000))
    if repository_id is not None:
        query = query.where(TeamTaskBinding.repository_id == repository_id)
    rows = [(row[0], row[1], row[2]) for row in (await session.execute(query)).all() if row[1].scope_paths]
    done = await _done(session, [row[0].task_id for row in rows])
    return [row for row in rows if str(row[0].task_id) not in done]


async def _live_leases(session: AsyncSession, project_id: uuid.UUID,
                       repository_id: int | None = None) -> list[TeamResourceLease]:
    query = select(TeamResourceLease).where(TeamResourceLease.project_id == project_id,
                                            TeamResourceLease.released_at.is_(None),
                                            TeamResourceLease.expires_at > _now()).order_by(TeamResourceLease.id)
    if repository_id is not None:
        query = query.where(TeamResourceLease.repository_id == repository_id)
    return list((await session.execute(query)).scalars().all())


def _related(first: TeamTaskPlan, second: TeamTaskPlan) -> bool:
    """Parent and child tasks share scope by construction; siblings and strangers are compared."""
    return first.parent_task_id == second.task_id or second.parent_task_id == first.task_id


def _conflicts(binding: TeamTaskBinding, plan: TeamTaskPlan | None,
               others: list[tuple[TeamTaskBinding, TeamTaskPlan, AgentTask]],
               leases: list[TeamResourceLease], paths: list[str] | None = None) -> list[dict]:
    """Overlaps of ``paths`` (default: the plan's declared scope) with other tasks' scopes and live leases."""
    if paths is not None:
        mine, extra = list(paths), {"source": "changed_paths"}
    else:
        mine, extra = list(plan.scope_paths or []) if plan is not None else [], {}
    warnings: list[dict] = []
    if not mine:
        return warnings
    for other_binding, other_plan, other_task in others:
        if (other_binding.task_id == binding.task_id or other_binding.repository_id != binding.repository_id
                or (plan is not None and _related(plan, other_plan))):
            continue
        pairs = overlapping(mine, list(other_plan.scope_paths or []))[:MAX_OVERLAP_PAIRS]
        if pairs:
            warnings.append({"kind": "task", "repository_id": binding.repository_id, "task_id": str(binding.task_id),
                             "other_task_id": str(other_binding.task_id), "other_title": other_task.title,
                             "paths": pairs, **extra})
    for lease in leases:
        if lease.repository_id != binding.repository_id or lease.task_id == binding.task_id:
            continue
        pairs = overlapping(mine, [lease.resource])[:MAX_OVERLAP_PAIRS]
        if pairs:
            warnings.append({"kind": "lease", "repository_id": binding.repository_id, "task_id": str(binding.task_id),
                             "lease_id": lease.id, "resource": lease.resource, "holder_id": lease.holder_id,
                             "expires_at": _iso(lease.expires_at), "paths": pairs, **extra})
    return warnings


async def _record_overlaps(session: AsyncSession, project_id: uuid.UUID, warnings: list[dict],
                           actor_id: int | None) -> None:
    for warning in warnings:
        changed = warning.get("source") == "changed_paths"
        fingerprint = digest({"paths": warning["paths"]})[:16]
        if warning["kind"] == "task":
            pair = sorted([warning["task_id"], warning["other_task_id"]])
            key = (f"overlap:changed:task:{pair[0]}:{pair[1]}" if changed
                   else f"overlap:task:{pair[0]}:{pair[1]}:{fingerprint}")
            summary = (f"Changed files overlap the scope of task {warning['other_title']}" if changed
                       else f"Scope overlap with task {warning['other_title']}")
        else:
            key = (f"overlap:changed:lease:{warning['task_id']}:{warning['lease_id']}" if changed
                   else f"overlap:lease:{warning['task_id']}:{warning['lease_id']}:{fingerprint}")
            summary = (f"Changed files overlap leased resource {warning['resource']}" if changed
                       else f"Scope overlaps leased resource {warning['resource']}")
        seen = (await session.execute(select(TeamActivityEvent.id).where(TeamActivityEvent.dedupe_key == key)
                                      .limit(1))).scalar_one_or_none()
        if seen is not None:
            continue
        try:
            async with session.begin_nested():
                await record_event(session, kind="coordination.overlap", summary=summary, audience="repository",
                                   actor_principal_id=actor_id, project_id=project_id,
                                   repository_id=warning["repository_id"], dedupe_key=key,
                                   details={key_: value for key_, value in warning.items() if key_ != "other_title"})
        except IntegrityError:
            continue  # a concurrent writer recorded the same overlap


async def detect_overlaps(session: AsyncSession, binding: TeamTaskBinding, plan: TeamTaskPlan, *,
                          actor_id: int | None, record: bool) -> list[dict]:
    if not plan.scope_paths:
        return []
    warnings = _conflicts(binding, plan, await _active_plans(session, binding.project_id, binding.repository_id),
                          await _live_leases(session, binding.project_id, binding.repository_id))
    if record and warnings:
        await _record_overlaps(session, binding.project_id, warnings, actor_id)
    return warnings


def changed_scope(paths: list[str] | None) -> list[str]:
    """Normalized, de-duplicated changed paths (at most ``MAX_CHANGED_PATHS``); unusable entries are skipped."""
    result: set[str] = set()
    for value in paths or []:
        try:
            result.add(normalize_changed_path(value))
        except TeamConflict:
            continue
    return sorted(result)[:MAX_CHANGED_PATHS]


async def record_change_overlaps(session: AsyncSession, task_id: uuid.UUID, changed_paths: list[str] | None, *,
                                 actor_id: int | None) -> list[dict]:
    """Compare the files a finished execution changed with other active tasks' scopes and live leases.

    Called by the executor in the transaction that records the run as
    completed. Each overlap is recorded once (the same ``coordination.overlap``
    dedupe as declared scopes) and returned."""
    paths = changed_scope(changed_paths)
    binding = await fresh(session, TeamTaskBinding, TeamTaskBinding.task_id == task_id)
    if binding is None or not paths:
        return []
    plan = (await session.execute(select(TeamTaskPlan).where(TeamTaskPlan.task_id == task_id))).scalar_one_or_none()
    warnings = _conflicts(binding, plan, await _active_plans(session, binding.project_id, binding.repository_id),
                          await _live_leases(session, binding.project_id, binding.repository_id), paths=paths)
    if warnings:
        await _record_overlaps(session, binding.project_id, warnings, actor_id)
    return warnings


async def overlaps_view(session: AsyncSession, project_id: uuid.UUID, repository_id: int | None = None) -> list[dict]:
    """Every current overlap among active tasks (each pair once) and between tasks and live leases."""
    active = await _active_plans(session, project_id, repository_id)
    leases = await _live_leases(session, project_id, repository_id)
    result = []
    for index, (binding, plan, _task) in enumerate(active):
        later = active[index + 1:]
        result.extend(_conflicts(binding, plan, later, leases))
    return result


async def list_overlaps(session: AsyncSession, caller: ResolvedPrincipal, project_id: uuid.UUID,
                        repository_id: int | None = None) -> list[dict]:
    await authorize(session, caller, project_id, Permission.READ)
    return await overlaps_view(session, project_id, repository_id)


# --------------------------------------------------------------------------- instruction resolution


async def resolve_instructions(session: AsyncSession, repo_path: str | None) -> list[dict]:
    """Canonical, active decisions and rules that apply to ``repo_path`` (plus the global ones), with versions."""
    spellings = repository_path_candidates(repo_path) if repo_path else []
    resolved: list[dict] = []
    for kind, model in (("decision", Decision), ("rule", Rule)):
        location = model.repo_path.is_(None) | model.repo_path.in_(spellings) if spellings else model.repo_path.is_(None)
        rows = (await session.execute(select(model.id, model.version).where(
            model.superseded_by_id.is_(None), model.status == "active", location)
            .order_by(model.id).limit(MAX_RESOLVED))).all()
        resolved.extend({"kind": kind, "id": row[0], "version": row[1]} for row in rows)
    return resolved


async def snapshot_instructions(session: AsyncSession, run: AgentRun) -> None:
    """Record what ``run`` resolved at start (called by ``store.request_run`` in its transaction)."""
    binding = await fresh(session, TeamTaskBinding, TeamTaskBinding.task_id == run.task_id)
    repository = await fresh(session, Repository, Repository.id == binding.repository_id) if binding else None
    session.add(TeamRunResolution(run_id=run.id, resolved=await resolve_instructions(
        session, repository.path if repository else None)))
    await session.flush()


async def superseded_instructions(session: AsyncSession, run_id: uuid.UUID) -> list[dict] | None:
    """Resolved instructions that changed since the run started; ``None`` when the run recorded none (pre-M3)."""
    row = await fresh(session, TeamRunResolution, TeamRunResolution.run_id == run_id)
    if row is None:
        return None
    stale: list[dict] = []
    for kind, model in (("decision", Decision), ("rule", Rule)):
        items = [item for item in row.resolved or [] if item.get("kind") == kind]
        if not items:
            continue
        current = {item[0]: item for item in (await session.execute(select(
            model.id, model.version, model.status, model.superseded_by_id).where(
            model.id.in_([item["id"] for item in items])))).all()}
        for item in items:
            now = current.get(item["id"])
            if now is None:
                stale.append({**item, "current_version": None, "reason": "deleted"})
            elif now[3] is not None or now[2] != "active":
                stale.append({**item, "current_version": now[1], "reason": "retired"})
            elif now[1] != item["version"]:
                stale.append({**item, "current_version": now[1], "reason": "edited"})
    return stale


async def get_run_instructions(session: AsyncSession, caller: ResolvedPrincipal, project_id: uuid.UUID,
                               task_id: uuid.UUID, run_id: uuid.UUID) -> dict:
    await authorize(session, caller, project_id, Permission.READ, task_id=task_id)
    run = await fresh(session, AgentRun, AgentRun.id == run_id, AgentRun.task_id == task_id)
    if run is None:
        raise TeamNotFound("Run not found")
    row = await fresh(session, TeamRunResolution, TeamRunResolution.run_id == run_id)
    stale = await superseded_instructions(session, run_id)
    status = "unknown" if stale is None else "superseded" if stale else "current"
    return {"run_id": str(run_id), "task_id": str(task_id), "status": status,
            "resolved_at": _iso(row.resolved_at) if row else None, "resolved": list(row.resolved or []) if row else [],
            "superseded": stale or []}


# --------------------------------------------------------------------------- critical leases


def lease_dict(lease: TeamResourceLease, now: datetime | None = None) -> dict:
    now = now or _now()
    state = ("released" if lease.released_at is not None
             else "held" if lease.expires_at > now else "expired")
    return {"id": lease.id, "project_id": str(lease.project_id), "repository_id": lease.repository_id,
            "resource": lease.resource, "holder_id": lease.holder_id,
            "task_id": str(lease.task_id) if lease.task_id else None, "fencing_token": lease.fencing_token,
            "state": state, "acquired_at": _iso(lease.acquired_at), "expires_at": _iso(lease.expires_at),
            "released_at": _iso(lease.released_at), "released_reason": lease.released_reason}


def _ttl(value: int | None) -> timedelta:
    seconds = 300 if value is None else int(value)
    if not MIN_LEASE_SECONDS <= seconds <= MAX_LEASE_SECONDS:
        raise TeamConflict(f"Lease duration must be {MIN_LEASE_SECONDS}-{MAX_LEASE_SECONDS} seconds")
    return timedelta(seconds=seconds)


async def acquire_lease(session: AsyncSession, caller: ResolvedPrincipal, project_id: uuid.UUID,
                        payload: dict) -> dict:
    """Take an exclusive lease, or take over an expired or released one (the fencing token increases)."""
    task_id = _uuid(payload["task_id"], "task id") if payload.get("task_id") else None
    authority = await authorize(session, caller, project_id, Permission.REQUEST_RUN, task_id=task_id)
    if authority.run_id is not None:
        run = await fresh(session, AgentRun, AgentRun.id == authority.run_id)
        task_id = run.task_id
    # Exclusive lock on the repository link serialises acquisitions, so nested paths cannot both be leased.
    repository = await fresh(session, ProjectRepository, ProjectRepository.repository_id == payload["repository_id"],
                             ProjectRepository.project_id == project_id, exclusive=True)
    if repository is None:
        raise TeamDenied("Repository access denied")
    binding = None
    if task_id is not None:
        binding = await _binding(session, project_id, task_id)
        if binding.repository_id != repository.repository_id:
            raise TeamConflict("Task belongs to another repository")
    resource = normalize_resource(payload["resource"])
    ttl = _ttl(payload.get("ttl_seconds"))
    now = _now()
    for lease in await _live_leases(session, project_id, repository.repository_id):
        if lease.resource != resource and paths_overlap(lease.resource, resource) and lease.holder_id != authority.actor_id:
            raise TeamConflict(f"Overlapping resource is leased: {lease.resource} (lease {lease.id})")
    row = await fresh(session, TeamResourceLease, TeamResourceLease.repository_id == repository.repository_id,
                      TeamResourceLease.resource == resource, exclusive=True)
    if row is None:
        try:
            async with session.begin_nested():
                row = TeamResourceLease(project_id=project_id, repository_id=repository.repository_id, resource=resource,
                                        holder_id=authority.actor_id, task_id=task_id, fencing_token=1,
                                        acquired_at=now, expires_at=now + ttl)
                session.add(row)
                await session.flush()
        except IntegrityError as exc:
            raise TeamConflict("Resource is being leased concurrently; retry") from exc
    elif row.released_at is None and row.expires_at > now:
        if row.holder_id != authority.actor_id:
            raise TeamConflict(f"Resource is leased until {row.expires_at.isoformat()}")
        # The holder repeating its acquisition (a retry) gets its live lease back unchanged.
    else:
        changed = await session.execute(update(TeamResourceLease).where(
            TeamResourceLease.id == row.id, TeamResourceLease.fencing_token == row.fencing_token)
            .values(holder_id=authority.actor_id, task_id=task_id, project_id=project_id,
                    fencing_token=row.fencing_token + 1, acquired_at=now, expires_at=now + ttl,
                    released_at=None, released_reason=None).returning(TeamResourceLease.id))
        if changed.scalar_one_or_none() is None:
            raise TeamConflict("Resource is being leased concurrently; retry")
        row = await fresh(session, TeamResourceLease, TeamResourceLease.id == row.id)
    warnings = []
    for other_binding, other_plan, other_task in await _active_plans(session, project_id, repository.repository_id):
        if other_binding.task_id == task_id:
            continue
        pairs = overlapping(list(other_plan.scope_paths or []), [resource])
        if pairs:
            warnings.append({"kind": "lease", "repository_id": repository.repository_id,
                             "task_id": str(other_binding.task_id), "lease_id": row.id, "resource": resource,
                             "holder_id": row.holder_id, "expires_at": _iso(row.expires_at), "paths": pairs})
    if warnings:
        await _record_overlaps(session, project_id, warnings, authority.actor_id)
    return {"lease": lease_dict(row, now), "warnings": warnings}


async def _held_lease(session: AsyncSession, project_id: uuid.UUID, lease_id: int, holder_id: int, token: int,
                      values: dict) -> TeamResourceLease:
    now = _now()
    changed = await session.execute(update(TeamResourceLease).where(
        TeamResourceLease.id == lease_id, TeamResourceLease.project_id == project_id,
        TeamResourceLease.holder_id == holder_id, TeamResourceLease.fencing_token == token,
        TeamResourceLease.released_at.is_(None), TeamResourceLease.expires_at > now)
        .values(**values).returning(TeamResourceLease.id))
    if changed.scalar_one_or_none() is None:
        if await fresh(session, TeamResourceLease, TeamResourceLease.id == lease_id,
                       TeamResourceLease.project_id == project_id) is None:
            raise TeamNotFound("Lease not found")
        raise TeamConflict("Lease is not held with this fencing token (expired, released or taken over)")
    return await fresh(session, TeamResourceLease, TeamResourceLease.id == lease_id)


async def renew_lease(session: AsyncSession, caller: ResolvedPrincipal, project_id: uuid.UUID, lease_id: int,
                      payload: dict) -> dict:
    authority = await authorize(session, caller, project_id, Permission.REQUEST_RUN)
    ttl = _ttl(payload.get("ttl_seconds"))
    lease = await _held_lease(session, project_id, lease_id, authority.actor_id, payload["fencing_token"],
                              {"expires_at": _now() + ttl})
    return lease_dict(lease)


async def release_lease(session: AsyncSession, caller: ResolvedPrincipal, project_id: uuid.UUID, lease_id: int,
                        payload: dict) -> dict:
    authority = await authorize(session, caller, project_id, Permission.READ)
    lease = await _held_lease(session, project_id, lease_id, authority.actor_id, payload["fencing_token"],
                              {"released_at": _now(), "released_reason": "released"})
    return lease_dict(lease)


async def force_release_lease(session: AsyncSession, caller: ResolvedPrincipal, project_id: uuid.UUID, lease_id: int,
                              payload: dict) -> dict:
    """A project administrator releases someone's lease, with a reason recorded in the activity feed.

    The fencing token increases, so the previous holder can no longer renew or
    release it (and any write it fences on that token is refused)."""
    await administrator(session, caller, project_id)
    reason = " ".join(str(payload.get("reason") or "").split())
    if not reason:
        raise TeamConflict("A reason is required to force-release a lease")
    lease = await fresh(session, TeamResourceLease, TeamResourceLease.id == lease_id,
                        TeamResourceLease.project_id == project_id, exclusive=True)
    if lease is None:
        raise TeamNotFound("Lease not found")
    if lease.released_at is not None:
        raise TeamConflict("Lease is already released")
    expected = payload.get("fencing_token")
    if expected is not None and int(expected) != lease.fencing_token:
        raise TeamConflict("Lease fencing token changed; reload the lease")
    now = _now()
    previous_holder, previous_token = lease.holder_id, lease.fencing_token
    changed = await session.execute(update(TeamResourceLease).where(
        TeamResourceLease.id == lease.id, TeamResourceLease.fencing_token == previous_token,
        TeamResourceLease.released_at.is_(None))
        .values(released_at=now, released_reason=f"force released: {reason}"[:64],
                fencing_token=previous_token + 1).returning(TeamResourceLease.id))
    if changed.scalar_one_or_none() is None:
        raise TeamConflict("Lease changed concurrently; retry")
    lease = await fresh(session, TeamResourceLease, TeamResourceLease.id == lease_id)
    await record_event(session, kind="coordination.lease_force_released",
                       summary=f"Lease on {lease.resource} force-released by an administrator",
                       audience="repository", actor_principal_id=caller.id, subject_principal_id=previous_holder,
                       project_id=project_id, repository_id=lease.repository_id,
                       details={"lease_id": lease.id, "resource": lease.resource, "holder_id": previous_holder,
                                "task_id": str(lease.task_id) if lease.task_id else None,
                                "previous_fencing_token": previous_token, "fencing_token": lease.fencing_token,
                                "reason": reason[:MAX_REASON]})
    return lease_dict(lease, now)


async def leases_view(session: AsyncSession, project_id: uuid.UUID, repository_id: int | None = None,
                      include_inactive: bool = False) -> list[dict]:
    query = select(TeamResourceLease).where(TeamResourceLease.project_id == project_id)
    if repository_id is not None:
        query = query.where(TeamResourceLease.repository_id == repository_id)
    if not include_inactive:
        query = query.where(TeamResourceLease.released_at.is_(None), TeamResourceLease.expires_at > _now())
    now = _now()
    return [lease_dict(row, now) for row in (await session.execute(query.order_by(TeamResourceLease.id).limit(500)))
            .scalars().all()]


async def list_leases(session: AsyncSession, caller: ResolvedPrincipal, project_id: uuid.UUID,
                      repository_id: int | None = None, include_inactive: bool = False) -> list[dict]:
    await authorize(session, caller, project_id, Permission.READ)
    return await leases_view(session, project_id, repository_id, include_inactive)


# --------------------------------------------------------------------------- integration queue


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


async def integrate(session: AsyncSession, caller: ResolvedPrincipal, project_id: uuid.UUID, entry_id: int,
                    payload: dict) -> dict:
    """Re-validate one entry against the current base and mark it integrated, or record why not.

    Requires task acceptance (Reviewer) by someone other than the run's
    initiator or requester. Entries are taken in queue order: an earlier entry
    still ``queued`` in the same repository must be integrated or rejected first.
    The outcome is returned (and committed) rather than raised, so a
    ``needs_rebase`` or ``stale_instructions`` verdict stays on record."""
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
    if any(item.base_sha == current_base and item.head_sha != current_base for item in integrated):
        raise TeamConflict("current_base_sha predates an integrated entry; use the current target revision")
    now = _now()
    stale: list[dict] = []
    if run.revoked_at is not None:
        status, reason = "rejected", "Run was revoked"
    elif entry.base_sha != current_base:
        status, reason = "needs_rebase", f"Run base {entry.base_sha[:12]} is not the current base {current_base[:12]}"
    else:
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
                              superseded=stale[:20])
    return {**entry_dict(entry, run), "superseded_instructions": stale}


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


__all__ = ["acquire_lease", "changed_scope", "create_subtask", "dispatch", "enqueue", "find_cycle",
           "force_release_lease", "get_plan", "get_run_instructions",
           "integrate", "leases_view", "list_leases", "list_overlaps", "list_queue", "normalize_path",
           "normalize_resource", "overlaps_view", "paths_overlap", "plan_view", "queue_view", "record_change_overlaps",
           "reject", "release_lease", "renew_lease", "require_dependencies_done",
           "resolve_instructions", "snapshot_instructions", "superseded_instructions", "update_plan"]
