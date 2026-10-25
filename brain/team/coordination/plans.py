"""Subtask plans: dependency graph, topological views, dispatch."""
from __future__ import annotations

import uuid
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from brain.auth.principals import ResolvedPrincipal
from brain.database.harness_models import AgentTask
from brain.team.authorization import Permission, TeamConflict, TeamDenied, authorize, fresh, permissions
from brain.team.models import AgentRun, TeamTaskBinding, TeamTaskPlan
from brain.team.store import create_task, request_run, task_dict
from brain.team.coordination._common import MAX_DEPENDENCIES, MAX_SUBTASKS, _done, _uuid, normalize_scope
from brain.team.coordination.overlaps import detect_overlaps


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
    warnings, graph = await detect_overlaps(session, binding, plan, actor_id=caller.id, record=True)
    return {**created, "plan": plan_dict(plan, task_id), "warnings": warnings, "dependency_graph": graph}


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
    warnings, graph = await detect_overlaps(session, binding, plan, actor_id=caller.id, record=True)
    return {"plan": plan_dict(plan, task_id), "warnings": warnings, "dependency_graph": graph}


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
