"""Overlap detection and repository-graph dependency lookups for team coordination."""
from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from brain.auth.principals import ResolvedPrincipal
from brain.database.harness_models import AgentTask
from brain.database.models import Repository
from brain.team._util import _iso, _now
from brain.team.activity import record_event
from brain.team.authorization import Permission, TeamConflict, authorize, fresh
from brain.team.models import TeamActivityEvent, TeamResourceLease, TeamTaskBinding, TeamTaskPlan
from brain.team.store import digest
from brain.team.coordination._common import (DEPENDENCY_TIMEOUT_SECONDS, INACTIVE_TASK_STATUSES, MAX_CHANGED_PATHS, MAX_DEPENDENCY_NEIGHBORS, MAX_DEPENDENCY_PATHS, MAX_OVERLAP_PAIRS, _GLOB, _done, _related, normalize_changed_path, overlapping, paths_overlap)


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


DependencyGraph = dict


DependencyProvider = Callable[[AsyncSession, int, list[str]], Awaitable[DependencyGraph]]


def _graph_result(status: str, reason: str | None = None, edges: dict[str, list[str]] | None = None,
                  truncated: bool = False) -> DependencyGraph:
    return {"status": status, "reason": reason, "edges": edges or {}, "truncated": truncated}


def _is_file_path(path: str) -> bool:
    """A concrete file the graph can name: no glob, no named resource, and an extension on the last segment."""
    return not path.startswith("resource:") and not _GLOB.search(path) and "." in path.rsplit("/", 1)[-1]


async def graph_dependencies(session: AsyncSession, repository_id: int, paths: list[str]) -> DependencyGraph:
    """One-hop ``IMPORTS`` neighbours (either direction) of each path, from the indexed repository graph.

    Returns ``status`` ``ok`` with ``edges``, ``stale`` when the index lags the
    source, or ``unavailable``; never raises. Bounded by
    ``MAX_DEPENDENCY_NEIGHBORS`` per path and ``DEPENDENCY_TIMEOUT_SECONDS``."""
    repo = await session.get(Repository, repository_id)
    if repo is None or not repo.path or not Path(repo.path).is_dir():
        return _graph_result("unavailable", "The repository source is not available to read its graph")

    async def lookup() -> DependencyGraph:
        from brain.graph.graph_client import GraphClient
        from brain.memory.repo_freshness import assess_repository_freshness

        freshness = (await assess_repository_freshness(repo)).get("status") or "unknown"
        if freshness in ("behind", "stale"):
            return _graph_result("stale", f"The repository index is {freshness}; re-index to compare imports")
        if freshness != "current":
            return _graph_result("unavailable", f"The repository index is {freshness}")
        client = GraphClient(repository_id)
        edges: dict[str, list[str]] = {}
        truncated = False
        for path in paths:
            found: set[str] = set()
            for neighbor in await client.get_import_neighbors(path, MAX_DEPENDENCY_NEIGHBORS + 1):
                target = neighbor.get("target") or {}
                props = target.get("properties") or {}
                name = props.get("name") or props.get("path")
                if ((neighbor.get("relationship") or {}).get("type") == "IMPORTS"
                        and "File" in (target.get("labels") or []) and isinstance(name, str) and name != path):
                    found.add(name)
            truncated = truncated or len(found) > MAX_DEPENDENCY_NEIGHBORS
            edges[path] = sorted(found)[:MAX_DEPENDENCY_NEIGHBORS]
        return _graph_result("ok", edges=edges, truncated=truncated)

    try:
        return await asyncio.wait_for(lookup(), DEPENDENCY_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        return _graph_result("unavailable", "The graph lookup timed out")
    except Exception as exc:  # noqa: BLE001 - a graph outage degrades the check, it never fails the write
        return _graph_result("unavailable", f"The graph lookup failed: {type(exc).__name__}")


# Tests replace this with a fake graph.
dependency_provider: DependencyProvider = graph_dependencies


def graph_report(repository_id: int, graph: DependencyGraph) -> dict:
    """What the response says about the dependency check: whether it ran, and why not."""
    return {"repository_id": repository_id, "status": graph["status"], "reason": graph.get("reason"),
            "truncated": bool(graph.get("truncated"))}


async def _repository_graph(session: AsyncSession, repository_id: int,
                            scopes: list[list[str]]) -> DependencyGraph:
    """Look up the files of ``scopes`` (the first one is the caller's, looked up first) within the bounds."""
    wanted: list[str] = []
    for scope in scopes:
        for path in sorted(scope):
            if _is_file_path(path) and path not in wanted:
                wanted.append(path)
    if not wanted:
        return _graph_result("skipped", "No declared scope names a file to look up in the import graph")
    graph = await dependency_provider(session, repository_id, wanted[:MAX_DEPENDENCY_PATHS])
    if len(wanted) > MAX_DEPENDENCY_PATHS:
        graph = {**graph, "truncated": True}
    return graph


def dependency_pairs(mine: list[str], theirs: list[str], edges: dict[str, list[str]]) -> list[list[str]]:
    """Import edges ``[my file, their file]`` between two scopes, sorted and capped at ``MAX_OVERLAP_PAIRS``."""
    pairs: set[tuple[str, str]] = set()
    for path in mine:
        for neighbor in edges.get(path, ()):
            if any(paths_overlap(neighbor, other) for other in theirs):
                pairs.add((path, neighbor))
    for path in theirs:
        for neighbor in edges.get(path, ()):
            if any(paths_overlap(neighbor, own) for own in mine):
                pairs.add((neighbor, path))
    return [list(pair) for pair in sorted(pairs)[:MAX_OVERLAP_PAIRS]]


def _dependency_conflicts(binding: TeamTaskBinding, plan: TeamTaskPlan,
                          others: list[tuple[TeamTaskBinding, TeamTaskPlan, AgentTask]],
                          edges: dict[str, list[str]], direct: list[dict]) -> list[dict]:
    """Dependency warnings for tasks that share no path with ``plan`` (a path overlap already says more)."""
    flagged = {warning["other_task_id"] for warning in direct if warning["kind"] == "task"}
    warnings: list[dict] = []
    for other_binding, other_plan, other_task in others:
        if (other_binding.task_id == binding.task_id or other_binding.repository_id != binding.repository_id
                or _related(plan, other_plan) or str(other_binding.task_id) in flagged):
            continue
        pairs = dependency_pairs(list(plan.scope_paths or []), list(other_plan.scope_paths or []), edges)
        if pairs:
            warnings.append({"kind": "dependency", "repository_id": binding.repository_id,
                             "task_id": str(binding.task_id), "other_task_id": str(other_binding.task_id),
                             "other_title": other_task.title, "relation": "imports", "paths": pairs})
    return warnings


async def _record_overlaps(session: AsyncSession, project_id: uuid.UUID, warnings: list[dict],
                           actor_id: int | None) -> None:
    for warning in warnings:
        changed = warning.get("source") == "changed_paths"
        if warning["kind"] == "dependency":
            pair = sorted([warning["task_id"], warning["other_task_id"]])
            canonical_paths = (warning["paths"] if warning["task_id"] == pair[0]
                               else [[right, left] for left, right in warning["paths"]])
            fingerprint = digest({"paths": canonical_paths})[:16]
            key = f"overlap:dependency:{pair[0]}:{pair[1]}:{fingerprint}"
            summary = f"Dependency overlap with task {warning['other_title']}"
        elif warning["kind"] == "task":
            fingerprint = digest({"paths": warning["paths"]})[:16]
            pair = sorted([warning["task_id"], warning["other_task_id"]])
            key = (f"overlap:changed:task:{pair[0]}:{pair[1]}" if changed
                   else f"overlap:task:{pair[0]}:{pair[1]}:{fingerprint}")
            summary = (f"Changed files overlap the scope of task {warning['other_title']}" if changed
                       else f"Scope overlap with task {warning['other_title']}")
        else:
            fingerprint = digest({"paths": warning["paths"]})[:16]
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
                          actor_id: int | None, record: bool) -> tuple[list[dict], dict]:
    """Path overlaps with other tasks and live leases, then import-dependency overlaps; and the graph status."""
    if not plan.scope_paths:
        return [], graph_report(binding.repository_id, _graph_result("skipped", "No declared scope"))
    active = await _active_plans(session, binding.project_id, binding.repository_id)
    warnings = _conflicts(binding, plan, active, await _live_leases(session, binding.project_id,
                                                                     binding.repository_id))
    others = [row for row in active if row[0].task_id != binding.task_id and not _related(plan, row[1])]
    if others:
        graph = await _repository_graph(session, binding.repository_id,
                                        [list(plan.scope_paths)] + [list(row[1].scope_paths or []) for row in others])
        if graph["status"] == "ok":
            warnings += _dependency_conflicts(binding, plan, others, graph["edges"], warnings)
    else:
        graph = _graph_result("skipped", "No other active task declares scope in this repository")
    if record and warnings:
        await _record_overlaps(session, binding.project_id, warnings, actor_id)
    return warnings, graph_report(binding.repository_id, graph)


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


async def overlaps_report(session: AsyncSession, project_id: uuid.UUID, repository_id: int | None = None) -> dict:
    """``overlaps_view`` plus import-dependency overlaps, with the graph status of each repository compared."""
    active = await _active_plans(session, project_id, repository_id)
    overlaps = await overlaps_view(session, project_id, repository_id)
    by_repository: dict[int, list[tuple[TeamTaskBinding, TeamTaskPlan, AgentTask]]] = {}
    for row in active:
        by_repository.setdefault(row[0].repository_id, []).append(row)
    graphs = []
    for repo_id in sorted(by_repository):
        rows = by_repository[repo_id]
        if len(rows) < 2:
            continue
        graph = await _repository_graph(session, repo_id, [list(row[1].scope_paths or []) for row in rows])
        graphs.append(graph_report(repo_id, graph))
        if graph["status"] != "ok":
            continue
        for index, (binding, plan, _task) in enumerate(rows):
            # overlaps_view lists each pair once, from the earlier task to the later ones, as compared here.
            direct = [item for item in overlaps if item["kind"] == "task" and item["task_id"] == str(binding.task_id)]
            overlaps.extend(_dependency_conflicts(binding, plan, rows[index + 1:], graph["edges"], direct))
    return {"overlaps": overlaps, "dependency_graphs": graphs}


async def list_overlaps(session: AsyncSession, caller: ResolvedPrincipal, project_id: uuid.UUID,
                        repository_id: int | None = None) -> dict:
    await authorize(session, caller, project_id, Permission.READ)
    return await overlaps_report(session, project_id, repository_id)
