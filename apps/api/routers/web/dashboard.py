"""Dashboard and agent-value endpoints."""
from __future__ import annotations

from typing import Any
from fastapi import HTTPException, Query
from loguru import logger
from ._common import REPOSITORY_READ, _resolve_repository, router


@router.get("/dashboard", dependencies=[REPOSITORY_READ])
async def web_dashboard(period: str = Query("24h"), repository_id: int | None = Query(None, ge=1)) -> dict[str, Any]:
    from apps.api.telemetry import dashboard_metrics
    days = {"24h": 1, "7d": 7, "30d": 30}.get(period)
    if days is None:
        raise HTTPException(status_code=400, detail="period must be one of 24h, 7d, 30d")
    if repository_id is not None:
        await _resolve_repository(repository_id)
    try:
        result = await dashboard_metrics(period_days=days, repository_id=repository_id)
    except Exception:
        # Dashboard observability must distinguish unavailable telemetry from an empty measured period.
        result = {
            "collection": {"collection_started_at": None, "until": None, "sample_count": None, "status": "unavailable"},
            "requests": {"total": None, "successes": None, "failures": None, "success_rate": None, "latency_ms": {"p50": None, "p95": None}, "clients": None, "by_operation": {}},
            "search": {"total": None, "successes": None, "failures": None, "sample_count": None, "latency_ms": {"p50": None, "p95": None}},
            "context": {"total": None, "successes": None, "failures": None, "sample_count": None, "latency_ms": {"p50": None, "p95": None}},
            "series": [],
        }
        unavailable = True
    else:
        unavailable = False
    result["scope"] = {"repository_id": repository_id, "mode": "repository" if repository_id is not None else "all"}
    result["collection_started_at"] = result["collection"]["collection_started_at"]
    result["observed_at"] = result["collection"]["until"]
    result["period"] = period
    result["partial"] = bool(result["collection"].get("partial", True))
    result["unavailable"] = unavailable
    result["total"] = result["requests"]["total"]
    result["failed"] = result["requests"]["failures"]
    result["clients"] = result["requests"]["clients"]
    result["context_total"] = result["context"]["total"]
    result["context_success"] = result["context"]["successes"]
    result["latency"] = {op: {"samples": data["sample_count"], "p50_ms": data["latency_ms"]["p50"], "p95_ms": data["latency_ms"]["p95"]} for op, data in (("search", result["search"]), ("context", result["context"]))}
    result["buckets"] = [{"at": item["bucket"], "total": item["requests"], "failed": item["failures"], "p95_ms": item.get("p95_ms"), "partial": item["partial"]} for item in result["series"]]
    result["recent_requests"] = result.get("recent_requests", [])
    return result


@router.get("/agent-value", dependencies=[REPOSITORY_READ])
async def web_agent_value(period: str = Query("30d"), repository_id: int | None = Query(None, ge=1)) -> dict[str, Any]:
    """Is Brain useful to the agents using it — adoption, pack economy, memory reach.

    Unavailable is reported as such. A dashboard that cannot tell a failed query
    from a measured zero would show "no agent used memory" when the truth is
    "nobody asked the database".
    """
    from brain.insights.agent_value import agent_value_metrics

    days = {"24h": 1, "7d": 7, "30d": 30, "90d": 90}.get(period)
    if days is None:
        raise HTTPException(status_code=400, detail="period must be one of 24h, 7d, 30d, 90d")
    if repository_id is not None:
        await _resolve_repository(repository_id)
    try:
        result = await agent_value_metrics(period_days=days, repository_id=repository_id)
    except Exception as exc:
        logger.warning("Agent value metrics unavailable: {}", type(exc).__name__)
        return {
            "period": period,
            "unavailable": True,
            "scope": {"repository_id": repository_id, "mode": "repository" if repository_id is not None else "all"},
            "window": {"days": days, "started_at": None, "until": None},
            "packs": None,
            "agents": [],
            "rules": None,
        }
    result["period"] = period
    result["unavailable"] = False
    result["scope"] = {"repository_id": repository_id, "mode": "repository" if repository_id is not None else "all"}
    return result
