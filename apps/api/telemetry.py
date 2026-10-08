"""Durable, payload-free request telemetry for owner dashboard metrics."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, text
from loguru import logger

from brain.database.models import BrainRequestTelemetry
from brain.database.session import async_session_factory


async def record_request(*, operation: str, repository_id: int | None, repository_path: str | None,
                         principal_name: str, request_id: str | None, outcome: str, latency_ms: float,
                         surface: str = "http") -> None:
    try:
        async with async_session_factory() as session:
            session.add(BrainRequestTelemetry(operation=operation, surface=surface, repository_id=repository_id,
                                              repository_path=repository_path, principal_name=principal_name,
                                              request_id=request_id, outcome=outcome,
                                              latency_ms=max(0.0, float(latency_ms))))
            await session.commit()
    except Exception as exc:
        # Observability must never make search/context unavailable.
        logger.warning("Request telemetry write failed: {}", type(exc).__name__)
        return


async def repository_id_for_path(path: str | None) -> int | None:
    if not path:
        return None
    from brain.database.models import Repository
    try:
        async with async_session_factory() as session:
            return (await session.execute(select(Repository.id).where(Repository.path == path))).scalar_one_or_none()
    except Exception:
        return None


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _ranked(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(len(ordered) * percentile) - 1)], 2)


FAILURES = {"error", "failed", "timeout", "cancelled"}


async def dashboard_metrics(*, period_days: int, repository_id: int | None = None) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=period_days)
    async with async_session_factory() as session:
        state = (await session.execute(text("SELECT started_at FROM brain_telemetry_state WHERE id = 1"))).first()
        if not state or not isinstance(state[0], datetime):
            raise RuntimeError("Request collector has not been initialized")
        collector_started = _utc(state[0])
        filters = [BrainRequestTelemetry.created_at >= start, BrainRequestTelemetry.created_at <= now]
        if repository_id is not None:
            filters.append(BrainRequestTelemetry.repository_id == repository_id)
        # Bound owner-dashboard work. Above this volume an aggregate SQL store is
        # needed; returning unavailable is preferable to silently sampling totals.
        rows = (await session.execute(select(
            BrainRequestTelemetry.operation, BrainRequestTelemetry.outcome,
            BrainRequestTelemetry.latency_ms, BrainRequestTelemetry.created_at,
            BrainRequestTelemetry.principal_name, BrainRequestTelemetry.repository_id,
            BrainRequestTelemetry.request_id, BrainRequestTelemetry.surface,
        ).where(*filters).limit(100_001))).all()
    if len(rows) > 100_000:
        raise RuntimeError("Request dashboard exceeds its aggregation capacity")

    def metrics(items):
        values = [float(row[2]) for row in items]
        return {"total": len(items), "successes": sum(row[1] == "success" for row in items),
                "failures": sum(row[1] in FAILURES for row in items),
                "partial": sum(row[1] == "partial" for row in items),
                "empty": sum(row[1] == "empty" for row in items), "sample_count": len(values),
                "latency_ms": {"p50": _ranked(values, .5), "p95": _ranked(values, .95)}}

    operations = {operation: metrics([row for row in rows if row[0] == operation]) for operation in ("search", "context")}
    requests = metrics(rows)
    requests.update({"success_rate": requests["successes"] / len(rows) if rows else None,
                     "clients": len({row[4] for row in rows if row[4] not in {"api:unattributed", "mcp:unattributed"}}),
                     "unattributed": sum(row[4] in {"api:unattributed", "mcp:unattributed"} for row in rows),
                     "by_operation": operations})
    delta = timedelta(hours=1) if period_days == 1 else timedelta(days=1)
    def bucket(at):
        value = _utc(at).replace(minute=0, second=0, microsecond=0)
        return value if period_days == 1 else value.replace(hour=0)
    grouped: dict[datetime, list[Any]] = {}
    for row in rows:
        grouped.setdefault(bucket(row[3]), []).append(row)
    cursor = bucket(start)
    series = []
    while cursor <= now:
        entries = grouped.get(cursor, [])
        unmeasured = cursor + delta <= collector_started
        value = metrics(entries)
        series.append({"bucket": cursor.isoformat(),
                       "requests": None if unmeasured else value["total"],
                       "successes": None if unmeasured else value["successes"],
                       "failures": None if unmeasured else value["failures"],
                       "p95_ms": None if unmeasured else value["latency_ms"]["p95"],
                       "partial": unmeasured or cursor < max(start, collector_started) or cursor + delta > now})
        cursor += delta
    return {"collection": {"collection_started_at": collector_started.isoformat(), "started_at": start.isoformat(),
                            "until": now.isoformat(), "sample_count": len(rows), "status": "measured",
                            "partial": collector_started > start},
            "requests": requests, "search": operations["search"], "context": operations["context"], "series": series,
            "recent_requests": [
                {"operation": row[0], "outcome": row[1], "latency_ms": round(float(row[2]), 2),
                 "recorded_at": _utc(row[3]).isoformat(), "repository_id": row[5], "request_id": row[6],
                 "surface": row[7], "identity": row[4]}
                for row in sorted(rows, key=lambda item: _utc(item[3]), reverse=True)[:20]
            ]}
