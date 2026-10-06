"""Operational status endpoints: /api/status*, /insights and
/api/status/recent-activity."""

from pathlib import Path

from fastapi import APIRouter, Depends
from sqlalchemy import select

from apps.api.auth import require_api_key, require_scope
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
from brain import __version__
from brain.config.paths import get_repo_root
from brain.database.models import BrainInsight
from brain.database.session import async_session_factory, check_health
from brain.indexers.repo_indexer import get_git_commit_hash

router = APIRouter()


@router.get("/api/status", dependencies=[Depends(require_api_key), Depends(require_scope("core:read"))])
async def api_overall_status():
    services = await check_health()
    is_healthy = all(svc.get("status") == "healthy" for svc in services.values())
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


@router.get("/api/status/diagnostics", dependencies=[Depends(require_api_key), Depends(require_scope("core:read"))])
async def diagnostics_status():
    return await get_self_diagnosis_status()


@router.get("/insights", dependencies=[Depends(require_api_key), Depends(require_scope("core:read"))])
async def list_insights(status: str | None = None, limit: int = 20):
    safe_limit = max(1, min(int(limit or 20), 100))
    async with async_session_factory() as session:
        stmt = select(BrainInsight).order_by(BrainInsight.last_seen_at.desc(), BrainInsight.id.desc()).limit(safe_limit)
        if status:
            stmt = stmt.where(BrainInsight.status == status)
        rows = (await session.execute(stmt)).scalars().all()
        return {
            "insights": [
                {
                    "id": row.id,
                    "insight_type": row.insight_type,
                    "severity": row.severity,
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
                    "created_at": format_datetime_utc(row.created_at),
                    "last_seen_at": format_datetime_utc(row.last_seen_at),
                }
                for row in rows
            ]
        }


@router.get("/api/status/services", dependencies=[Depends(require_api_key), Depends(require_scope("core:read"))])
async def api_services_status():
    return await check_health()


@router.get("/api/status/indexing", dependencies=[Depends(require_api_key), Depends(require_scope("core:read"))])
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


@router.get("/api/status/graph", dependencies=[Depends(require_api_key), Depends(require_scope("core:read"))])
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


@router.get("/api/status/mcp", dependencies=[Depends(require_api_key), Depends(require_scope("core:read"))])
async def api_mcp_status():
    runtime = await get_mcp_runtime_status()
    tools = runtime.get("tools", [])
    available = bool(runtime.get("available"))
    server_status = runtime.get("server_status", "online" if available else "unavailable")
    return {
        "server_status": server_status,
        "available": available,
        "total_tools": len(tools),
        "tools": tools,
        "status": runtime.get("status", server_status),
        "diagnostics": runtime.get("diagnostics", {}),
    }


@router.get("/api/status/mcp/readiness", dependencies=[Depends(require_api_key), Depends(require_scope("core:read"))])
async def api_mcp_readiness_status(timeout_seconds: float = 30.0):
    return await get_mcp_readiness_status(timeout_seconds=timeout_seconds)


@router.get("/api/status/recent-activity", dependencies=[Depends(require_api_key), Depends(require_scope("core:read"))])
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
