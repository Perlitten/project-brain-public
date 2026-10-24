"""Instruction resolution: the decision/rule versions a run was planned against."""
from __future__ import annotations

import uuid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from brain.auth.principals import ResolvedPrincipal
from brain.database.models import Decision, Repository, Rule
from brain.database.repository_utils import repository_path_candidates
from brain.team._util import _iso
from brain.team.authorization import Permission, authorize, fresh
from brain.team.models import AgentRun, TeamRunResolution, TeamTaskBinding
from brain.team.provisioning import TeamNotFound
from brain.team.coordination._common import MAX_RESOLVED


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
