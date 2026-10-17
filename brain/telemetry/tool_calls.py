"""Per-tool MCP call telemetry: record each call, summarise health per tool."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, cast

from loguru import logger
from sqlalchemy import CursorResult, delete, select

from brain.database.models import McpToolCall
from brain.database.session import async_session_factory

WINDOW_HOURS = 24


async def record_tool_call(tool: str, surface: str, outcome: str, latency_ms: float) -> None:
    """Persist one call. Best-effort: telemetry must never break a tool call."""
    try:
        async with async_session_factory() as session:
            session.add(McpToolCall(tool=tool[:64], surface=surface[:16], outcome=outcome[:16],
                                    latency_ms=float(latency_ms)))
            await session.commit()
    except Exception as exc:  # pragma: no cover - depends on a live database
        logger.debug(f"MCP tool telemetry not recorded: {type(exc).__name__}")


async def prune_tool_calls(retention_days: int) -> int:
    """Delete calls older than the retention window; return how many. Only the
    last WINDOW_HOURS are ever read, so older rows are dead weight."""
    if retention_days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(retention_days, math.ceil(WINDOW_HOURS / 24)))
    async with async_session_factory() as session:
        result = await session.execute(delete(McpToolCall).where(McpToolCall.created_at < cutoff))
        await session.commit()
    return int(cast(CursorResult[Any], result).rowcount or 0)


def _ranked(values: list[float], percentile: float) -> float | None:
    """Nearest-rank percentile, computed in Python so SQLite (Lite mode) works too."""
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return ordered[index]


async def summarize_tool_calls(window_hours: int = WINDOW_HOURS) -> dict[str, dict[str, Any]]:
    """calls, error rate and latency percentiles per tool over the window."""
    since = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    stmt = select(McpToolCall.tool, McpToolCall.outcome, McpToolCall.latency_ms).where(McpToolCall.created_at >= since)
    async with async_session_factory() as session:
        rows = (await session.execute(stmt)).all()
    grouped: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for tool, outcome, latency in rows:
        grouped[str(tool)].append((str(outcome), float(latency)))
    summary: dict[str, dict[str, Any]] = {}
    for tool, calls in grouped.items():
        errors = sum(1 for outcome, _ in calls if outcome != "success")
        latencies = [latency for _, latency in calls]
        p50, p95 = _ranked(latencies, 0.5), _ranked(latencies, 0.95)
        summary[tool] = {
            "calls_24h": len(calls),
            "errors_24h": errors,
            "error_rate": round(errors / len(calls), 4),
            "p50_ms": round(p50, 1) if p50 is not None else None,
            "p95_ms": round(p95, 1) if p95 is not None else None,
        }
    return summary
