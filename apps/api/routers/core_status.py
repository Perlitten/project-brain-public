"""Operational status endpoints: /api/status*, /insights and
/api/status/recent-activity."""

from typing import Any
from pathlib import Path
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import case, func, or_, select, update

from apps.api.auth import require_api_key, require_principal, require_scope
from apps.api.helpers import (
    format_datetime_utc,
    get_cache_status,
    _short_commit_ref,
    get_db_counts,
    get_embedding_retrieval_health,
    get_latest_critic_status,
    get_latest_eval_metrics,
    get_mcp_runtime_status,
    get_mcp_readiness_status,
    get_neo4j_counts,
    get_reports_list,
    get_self_diagnosis_status,
    get_top_connected_nodes,
)
from apps.api.team_access import repository_authority, is_team_administrator, repository_access, team_administrator_access
from brain import __version__
from brain.auth.principals import ResolvedPrincipal
from brain.config.paths import get_repo_root
from brain.database.models import BrainInsight, File, FileChunk, Repository
from brain.insights.evidence import code_target
from brain.database.session import async_session_factory, check_health
from brain.indexers.repo_indexer import get_git_commit_hash
from brain.team.authorization import Permission

router = APIRouter()

# Operator detail across every repository (runs, commits, graph hubs, reports):
# a team administrator once the Brain has members. Liveness (/api/status,
# /api/status/services, /api/status/mcp) stays readable by every member.
ADMIN_READ = [Depends(require_api_key), Depends(require_scope("core:read")), Depends(team_administrator_access())]


@router.get("/api/status", dependencies=[Depends(require_api_key), Depends(require_scope("core:read"))])
async def api_overall_status(principal: ResolvedPrincipal = Depends(require_principal)):
    services = await check_health()
    is_healthy = all(svc.get("status") == "healthy" for svc in services.values())
    if not await is_team_administrator(principal):
        # A member sees liveness only: server checkout path, commit and
        # every-repository counts are team-administrator detail.
        return {"status": "ok" if is_healthy else "error", "version": __version__, "services": services}
    repo_path = get_repo_root()
    commit_hash = get_git_commit_hash(repo_path)

    async with async_session_factory() as session:
        counts = await get_db_counts(session)

    return {
        "status": "ok" if is_healthy else "error",
        "version": __version__,
        "services": services,
        "repo_path": repo_path.as_posix(),
        "commit_hash": commit_hash,
        "counts": counts,
    }


@router.get("/api/status/diagnostics", dependencies=ADMIN_READ)
async def diagnostics_status():
    return await get_self_diagnosis_status()


# Insights have no repository owner yet: the unscoped list is a team-administrator
# view, and a repository-scoped request (always empty for now) needs project read.
@router.get("/insights", dependencies=[Depends(require_api_key), Depends(require_scope("core:read")),
                                       Depends(repository_access(Permission.READ))])
async def list_insights(
    status: str | None = Query(None, max_length=50),
    limit: int = Query(20, ge=1, le=500),
    page: int = Query(1, ge=1),
    page_size: int | None = Query(None, ge=1, le=500),
    q: str | None = Query(None, max_length=500),
    severity: str | None = Query(None, max_length=20),
    repository_id: int | None = Query(None),
):
    # Direct unit callers bypass FastAPI's Query coercion.
    if not isinstance(status, str):
        status = None
    if not isinstance(q, str):
        q = None
    if not isinstance(severity, str):
        severity = None
    if not isinstance(repository_id, int):
        repository_id = None
    size_value = page_size if isinstance(page_size, int) else limit if isinstance(limit, int) else 20
    safe_limit = max(1, min(size_value, 500))
    safe_page = max(1, page if isinstance(page, int) else 1)

    def tone(value: str | None) -> str:
        raw = (value or "").lower()
        if raw in {"high", "critical", "error", "bad"}:
            return "bad"
        if raw in {"medium", "warning", "warn"}:
            return "warn"
        return "info"

    async with async_session_factory() as session:
        # BrainInsight currently has no repository ownership column. An
        # explicit repository request therefore fails closed instead of
        # presenting global findings as repository-specific.
        if repository_id is not None:
            return {
                "insights": [],
                "total": 0,
                "page": safe_page,
                "page_size": safe_limit,
                "total_pages": 1,
                "facets": {"status": {}},
                "scope": "repository",
            }
        stmt = select(BrainInsight).order_by(BrainInsight.last_seen_at.desc(), BrainInsight.id.desc())
        if status:
            stmt = stmt.where(BrainInsight.status == status)
        else:
            stmt = stmt.where(BrainInsight.status.not_in(("resolved", "dismissed", "closed", "archived")))
        if q:
            needle = f"%{q.strip()}%"
            stmt = stmt.where(BrainInsight.title.ilike(needle) | BrainInsight.summary.ilike(needle))
        severity_expr = case(
            (func.lower(BrainInsight.severity).in_(("high", "critical", "error", "bad")), "bad"),
            (func.lower(BrainInsight.severity).in_(("medium", "warning", "warn")), "warn"),
            else_="info",
        ).label("severity_tone")
        facet_where = stmt.whereclause
        facet_stmt = select(severity_expr, func.count()).select_from(BrainInsight)
        if facet_where is not None:
            facet_stmt = facet_stmt.where(facet_where)
        facets = dict(
            (str(k), int(v))
            for k, v in (
                await session.execute(
                    facet_stmt.group_by(severity_expr)
                )
            ).all()
        )
        if severity:
            if severity not in {"bad", "warn", "info"}:
                return {
                    "insights": [],
                    "total": 0,
                    "page": safe_page,
                    "page_size": safe_limit,
                    "total_pages": 1,
                    "facets": {"severity": facets},
                    "scope": "global",
                }
            stmt = stmt.where(severity_expr == severity)
        total = int((await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar() or 0)
        rows = (await session.execute(stmt.offset((safe_page - 1) * safe_limit).limit(safe_limit))).scalars().all()
        return {
            "insights": [
                {
                    "id": row.id,
                    "insight_type": row.insight_type,
                    "severity": tone(row.severity),
                    "raw_severity": row.severity,
                    "title": row.title,
                    "summary": row.summary,
                    "evidence": row.evidence or [],
                    "recommended_action": row.recommended_action,
                    "confidence": row.confidence,
                    "status": row.status,
                    "source": row.source,
                    "source_model": row.source_model,
                    "engine_version": row.engine_version,
                    "occurrence_count": row.occurrence_count,
                    "dedupe_key": getattr(row, "dedupe_key", None),
                    "code_available": code_target(row.evidence or []) is not None,
                    "dismissal": {"reason": getattr(row, "dismissed_reason", None), "by": getattr(row, "dismissed_by", None),
                                  "at": format_datetime_utc(getattr(row, "dismissed_at", None)),
                                  "evidence": getattr(row, "dismissed_evidence", None)},
                    "created_at": format_datetime_utc(row.created_at),
                    "last_seen_at": format_datetime_utc(row.last_seen_at),
                }
                for row in rows
            ],
            "total": total,
            "page": safe_page,
            "page_size": safe_limit,
            "total_pages": max(1, (total + safe_limit - 1) // safe_limit),
            "facets": {"severity": facets},
            "scope": "global",
        }


class DismissInsightRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)
    expected_last_seen_at: datetime


@router.post("/insights/{insight_id}/dismiss", dependencies=[Depends(require_api_key),
             Depends(require_scope("core:write")), Depends(team_administrator_access())])
async def dismiss_insight(insight_id: int, body: DismissInsightRequest,
                          principal: ResolvedPrincipal = Depends(require_principal)) -> dict[str, Any]:
    # Findings currently have global ownership. Only an operator/team admin
    # can triage them; do not infer a project from a module or a client actor.
    reason = body.reason.strip()
    if not reason:
        raise HTTPException(status_code=400, detail="A dismissal reason is required")
    if body.expected_last_seen_at.tzinfo is None:
        raise HTTPException(status_code=400, detail="The finding version must include a timezone")
    async with async_session_factory() as session, session.begin():
        row = await session.get(BrainInsight, insight_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Finding not found")
        if row.status == "dismissed":
            return {"status": "dismissed", "id": insight_id, "already_dismissed": True}
        from brain.insights.proactive import is_legal_status_transition
        if not is_legal_status_transition(row.status, "dismissed"):
            raise HTTPException(status_code=409, detail="This finding is no longer open for dismissal")
        changed = await session.execute(update(BrainInsight).where(
            BrainInsight.id == insight_id,
            BrainInsight.last_seen_at == body.expected_last_seen_at,
            BrainInsight.status.not_in(("dismissed", "resolved", "closed", "archived")),
        ).values(status="dismissed", dismissed_reason=reason, dismissed_by=principal.name,
                 dismissed_at=datetime.now(timezone.utc), dismissed_evidence=row.evidence,
                 dismissed_last_seen_at=row.last_seen_at).returning(BrainInsight.id))
        if changed.scalar_one_or_none() is None:
            raise HTTPException(status_code=409, detail="Finding changed; refresh and review its latest evidence before dismissing")
    return {"status": "dismissed", "id": insight_id, "already_dismissed": False}


@router.get("/insights/{insight_id}/code", dependencies=[Depends(require_api_key),
            Depends(require_scope("core:read")), Depends(team_administrator_access())])
async def insight_code(insight_id: int, principal: ResolvedPrincipal = Depends(require_principal)) -> dict[str, Any]:
    async with async_session_factory() as session:
        row = await session.get(BrainInsight, insight_id)
        target = code_target(row.evidence or []) if row else None
        if target is None:
            raise HTTPException(status_code=404, detail="Finding has no repository-bound file evidence")
        repository_id, path, line = target
        # Operator access to global findings does not grant source access to
        # a project where this person has no live role.
        await repository_authority(principal, Permission.READ, repository_id=repository_id)
        repository = await session.get(Repository, repository_id)
        file = (await session.execute(select(File).where(File.repository_id == repository_id, File.path == path))).scalar_one_or_none()
        if repository is None or file is None:
            raise HTTPException(status_code=404, detail="Evidence file is not in the repository index")
        chunks = (await session.execute(select(
            FileChunk.start_line, FileChunk.end_line, func.substr(FileChunk.content, 1, 12000).label("content"),
        ).where(FileChunk.file_id == file.id, or_(FileChunk.start_line.is_(None),
            (FileChunk.start_line <= line + 40) & (FileChunk.end_line >= max(1, line - 20))))
          .order_by(FileChunk.chunk_index).limit(3))).all()
    if not chunks:
        raise HTTPException(status_code=404, detail="No indexed code covers this evidence location")
    return {"repository": repository.name, "repository_id": repository_id, "path": path, "line": line,
            "revision": repository.last_indexed_commit, "snapshot": True,
            "chunks": [{"start_line": chunk.start_line, "end_line": chunk.end_line, "content": chunk.content} for chunk in chunks]}


@router.get("/api/status/services", dependencies=[Depends(require_api_key), Depends(require_scope("core:read"))])
async def api_services_status():
    return await check_health()


@router.get("/api/status/indexing", dependencies=ADMIN_READ)
async def api_indexing_status():
    from brain.database.models import IndexingRun

    async with async_session_factory() as session:
        counts = await get_db_counts(session)
        stmt = select(IndexingRun).order_by(IndexingRun.id.desc()).limit(5)
        res = await session.execute(stmt)
        runs = res.scalars().all()
        runs_list = [
            {
                "id": run.id,
                "started_at": format_datetime_utc(run.started_at),
                "commit_hash": run.commit_hash,
                "status": run.status,
                "completed_at": format_datetime_utc(run.completed_at),
                "repository_id": run.repository_id,
                "updated_at": format_datetime_utc(getattr(run, "updated_at", None)),
                "progress": getattr(run, "progress", None) or {},
                "verification": getattr(run, "verification", None),
            }
            for run in runs
        ]

    embedding_health = await get_embedding_retrieval_health()

    return {
        "counts": counts,
        "recent_runs": runs_list,
        "evaluation_score": get_latest_eval_metrics(),
        "cache_status": get_cache_status(),
        "embedding_health": embedding_health,
    }


@router.get("/api/status/graph", dependencies=ADMIN_READ)
async def api_graph_status():
    services = await check_health()
    counts = await get_neo4j_counts()
    top_connected, top_error = await get_top_connected_nodes(5)
    return {
        "neo4j_status": services.get("neo4j"),
        "counts": counts,
        "top_connected": top_connected,
        "top_connected_error": top_error,
        "cache_status": get_cache_status(),
        "evaluation_score": get_latest_eval_metrics(),
    }


async def _tool_usage() -> dict | None:
    """Per-tool call stats, or None when telemetry cannot be read (shown as 'not measured')."""
    try:
        from brain.telemetry.tool_calls import summarize_tool_calls

        return await summarize_tool_calls()
    except Exception:
        return None


@router.get("/api/status/mcp", dependencies=[Depends(require_api_key), Depends(require_scope("core:read"))])
async def api_mcp_status():
    runtime = await get_mcp_runtime_status()
    tools = runtime.get("tools", [])
    usage = await _tool_usage()
    if usage is not None:
        # Per-tool health over the last 24 hours; a tool nobody called reports 0 calls.
        tools = [{**tool, **usage.get(str(tool.get("name")), {"calls_24h": 0, "errors_24h": 0, "error_rate": 0.0})}
                 for tool in tools]
    available = bool(runtime.get("available"))
    server_status = runtime.get("server_status", "online" if available else "unavailable")
    return {
        "server_status": server_status,
        "available": available,
        "total_tools": len(tools),
        "tools": tools,
        "status": runtime.get("status", server_status),
        "diagnostics": runtime.get("diagnostics", {}),
        "usage_measured": usage is not None,
    }


@router.get("/api/status/mcp/readiness", dependencies=ADMIN_READ)
async def api_mcp_readiness_status(timeout_seconds: float = 30.0):
    return await get_mcp_readiness_status(timeout_seconds=timeout_seconds)


@router.get("/api/status/recent-activity", dependencies=ADMIN_READ)
async def api_recent_activity():
    from brain.database.models import ContextPack, IndexingRun

    reports = get_reports_list()
    async with async_session_factory() as session:
        stmt_idx = select(IndexingRun).order_by(IndexingRun.id.desc()).limit(3)
        res_idx = await session.execute(stmt_idx)
        runs = res_idx.scalars().all()

        stmt_cp = select(ContextPack).order_by(ContextPack.id.desc()).limit(3)
        res_cp = await session.execute(stmt_cp)
        packs = res_cp.scalars().all()

    activity = []
    latest_critic = "UNKNOWN"

    for run in runs:
        activity.append(
            {
                "title": f"Indexing Run #{run.id}",
                "timestamp": format_datetime_utc(run.started_at),
                "description": f"Status: {run.status}, Commit: {_short_commit_ref(run.commit_hash)}",
            }
        )
    for pack in packs:
        critic_status = get_latest_critic_status(pack.path)
        if latest_critic == "UNKNOWN":
            latest_critic = critic_status
        activity.append(
            {
                "title": f"Context Pack #{pack.id}",
                "timestamp": format_datetime_utc(pack.created_at),
                "description": f"Generated path: {Path(pack.path).name}, Critic: {critic_status}",
            }
        )
    for report in reports[:3]:
        activity.append(
            {
                "title": f"Report: {report['name']}",
                "timestamp": report["created_at"],
                "description": f"Size: {report['size']} bytes",
            }
        )
    activity.sort(key=lambda item: item["timestamp"] or "", reverse=True)

    return {
        "activity": activity[:10],
        "latest_critic_status": latest_critic,
        "evaluation_score": get_latest_eval_metrics(),
    }
