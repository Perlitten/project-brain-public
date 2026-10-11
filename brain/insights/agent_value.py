"""Whether Brain is actually useful to the agents using it.

Speed is easy to measure and the least interesting thing to know. What an owner
needs to see is whether agents come back, whether the packs they get are small
enough to be worth reading, and whether the rules and decisions anybody
bothered to write down ever reach the work.

Three honesty rules run through every number here, because a dashboard that
rounds absence down to zero is worse than no dashboard:

- A column that was never recorded is ``None``, never ``0``. Pack composition
  has only been recorded since the accounting columns shipped; every older row
  counts as ``unmeasured`` and is excluded from shares, not averaged in as if
  it had delivered nothing.
- Each section reports when its own measurement began, so a small number can
  be read as "new" rather than "bad".
- Aggregation is bounded. Past the cap this raises instead of sampling, for
  the same reason as the request dashboard: a silently partial total is
  indistinguishable from a real one.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional, Sequence

from sqlalchemy import func, select

from brain.database.models import BrainRequestTelemetry, ContextPack, Rule, RuleUsageEvent
from brain.database.session import async_session_factory
from brain.memory.rule_usage import DELIVERED, FLAGGED

# Above this many rows in the window the aggregate belongs in SQL, not in
# Python. Raising is deliberate: see the module docstring.
ROW_CAP = 100_000

UNATTRIBUTED = {"api:unattributed", "mcp:unattributed"}
FAILURES = {"error", "failed", "timeout", "cancelled"}


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return _utc(value).isoformat() if isinstance(value, datetime) else None


def _percentile(values: Sequence[float], percentile: float) -> Optional[float]:
    """Nearest-rank percentile, or ``None`` when nothing was measured."""
    if not values:
        return None
    ordered = sorted(values)
    return round(float(ordered[max(0, math.ceil(len(ordered) * percentile) - 1)]), 2)


def _share(part: int, whole: int) -> Optional[float]:
    """A proportion, or ``None`` when there is nothing to take a share of."""
    return round(part / whole, 4) if whole else None


def _spread(values: Iterable[Optional[int]]) -> dict[str, Any]:
    """Typical and worst case for one recorded count, with its sample size."""
    measured = [float(v) for v in values if v is not None]
    return {"p50": _percentile(measured, 0.5), "p95": _percentile(measured, 0.95), "sample_count": len(measured)}


def _agent_label(name: Optional[str]) -> str:
    return name if name and name not in UNATTRIBUTED else "unattributed"


async def agent_value_metrics(*, period_days: int = 30, repository_id: Optional[int] = None) -> dict[str, Any]:
    """Adoption, pack economy, memory reach and rule efficacy over one window.

    ``repository_id`` narrows packs and rule events to one repository. Request
    telemetry is narrowed the same way, so a repository view never mixes in
    another project's traffic.
    """
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=max(1, period_days))

    async with async_session_factory() as session:
        pack_filters = [ContextPack.created_at >= start, ContextPack.created_at <= now]
        if repository_id is not None:
            pack_filters.append(ContextPack.repository_id == repository_id)
        packs = (
            await session.execute(
                select(
                    ContextPack.created_at,
                    ContextPack.consumer,
                    ContextPack.token_estimate,
                    ContextPack.file_count,
                    ContextPack.rule_count,
                    ContextPack.decision_count,
                    ContextPack.learning_count,
                    ContextPack.budget,
                )
                .where(*pack_filters)
                .limit(ROW_CAP + 1)
            )
        ).all()
        if len(packs) > ROW_CAP:
            raise RuntimeError("Agent value dashboard exceeds its aggregation capacity")

        # When pack accounting began, read from the data itself rather than a
        # migration date: the first pack that carries a recorded token estimate.
        pack_measurement_started = (
            await session.execute(select(func.min(ContextPack.created_at)).where(ContextPack.token_estimate.isnot(None)))
        ).scalar_one_or_none()

        request_filters = [BrainRequestTelemetry.created_at >= start, BrainRequestTelemetry.created_at <= now]
        if repository_id is not None:
            request_filters.append(BrainRequestTelemetry.repository_id == repository_id)
        requests = (
            await session.execute(
                select(
                    BrainRequestTelemetry.principal_name,
                    BrainRequestTelemetry.operation,
                    BrainRequestTelemetry.outcome,
                    BrainRequestTelemetry.surface,
                    BrainRequestTelemetry.created_at,
                )
                .where(*request_filters)
                .limit(ROW_CAP + 1)
            )
        ).all()
        if len(requests) > ROW_CAP:
            raise RuntimeError("Agent value dashboard exceeds its aggregation capacity")

        event_filters = [RuleUsageEvent.created_at >= start, RuleUsageEvent.created_at <= now]
        if repository_id is not None:
            event_filters.append(RuleUsageEvent.repository_id == repository_id)
        events = (
            await session.execute(
                select(RuleUsageEvent.rule_id, RuleUsageEvent.kind, RuleUsageEvent.created_at)
                .where(*event_filters)
                .limit(ROW_CAP + 1)
            )
        ).all()
        if len(events) > ROW_CAP:
            raise RuntimeError("Agent value dashboard exceeds its aggregation capacity")

        rule_measurement_started = (
            await session.execute(select(func.min(RuleUsageEvent.created_at)))
        ).scalar_one_or_none()
        active_rules = (
            await session.execute(
                select(func.count()).select_from(Rule).where(func.coalesce(Rule.status, "active") == "active")
            )
        ).scalar_one_or_none() or 0

    return {
        "window": {"days": period_days, "started_at": start.isoformat(), "until": now.isoformat()},
        "packs": _pack_section(packs, pack_measurement_started, start),
        "agents": _agent_section(requests, packs),
        "rules": _rule_section(events, rule_measurement_started, int(active_rules)),
    }


def _pack_section(packs: Sequence[Any], measurement_started: Optional[datetime], start: datetime) -> dict[str, Any]:
    """Pack economy and memory reach, over packs built in the window."""
    measured = [p for p in packs if p[2] is not None]
    # "Carried memory" is only answerable for packs whose composition was
    # recorded. A pack built before that is not a pack that carried nothing.
    with_memory = [p for p in measured if (p[4] or 0) + (p[5] or 0) + (p[6] or 0) > 0]
    return {
        "total": len(packs),
        "measured": len(measured),
        "unmeasured": len(packs) - len(measured),
        "measurement_started_at": _iso(measurement_started),
        # The window opened before any pack was measured, so a low share here
        # says "not measured yet", not "packs are empty".
        "partial": measurement_started is None or _utc(measurement_started) > start,
        "tokens": _spread(p[2] for p in packs),
        "files": _spread(p[3] for p in packs),
        "rules_per_pack": _spread(p[4] for p in packs),
        "decisions_per_pack": _spread(p[5] for p in packs),
        "learnings_per_pack": _spread(p[6] for p in packs),
        "with_memory": len(with_memory) if measured else None,
        "memory_reach": _share(len(with_memory), len(measured)),
        "carrying": {
            "rules": sum((p[4] or 0) > 0 for p in measured) if measured else None,
            "decisions": sum((p[5] or 0) > 0 for p in measured) if measured else None,
            "learnings": sum((p[6] or 0) > 0 for p in measured) if measured else None,
        },
        "attributed": sum(1 for p in packs if p[1]),
        "unattributed": sum(1 for p in packs if not p[1]),
        "by_budget": _counted(str(p[7] or "unrecorded") for p in packs),
    }


def _counted(values: Iterable[str]) -> list[dict[str, Any]]:
    tally: dict[str, int] = {}
    for value in values:
        tally[value] = tally.get(value, 0) + 1
    return [{"name": name, "count": count} for name, count in sorted(tally.items(), key=lambda kv: -kv[1])]


def _agent_section(requests: Sequence[Any], packs: Sequence[Any]) -> list[dict[str, Any]]:
    """One row per caller: how much it asks for, and what it got.

    ``pack_rate`` is the share of this agent's context requests that produced a
    durable pack. It is not a task count: an agent can take runtime context
    without a pack being persisted, and that is a legitimate outcome — the
    number is here to show which callers get durable context and which do not.
    """
    agents: dict[str, dict[str, Any]] = {}

    def row(name: str) -> dict[str, Any]:
        return agents.setdefault(
            name,
            {
                "agent": name,
                "requests": 0,
                "context_requests": 0,
                "search_requests": 0,
                "successes": 0,
                "failures": 0,
                "surfaces": set(),
                "packs": 0,
                "packs_with_memory": None,
                "last_seen_at": None,
            },
        )

    for principal, operation, outcome, surface, created_at in requests:
        entry = row(_agent_label(principal))
        entry["requests"] += 1
        if operation == "context":
            entry["context_requests"] += 1
        elif operation == "search":
            entry["search_requests"] += 1
        if outcome == "success":
            entry["successes"] += 1
        elif outcome in FAILURES:
            entry["failures"] += 1
        if surface:
            entry["surfaces"].add(surface)
        seen = _iso(created_at)
        if seen and (entry["last_seen_at"] is None or seen > entry["last_seen_at"]):
            entry["last_seen_at"] = seen

    for pack in packs:
        created_at, consumer, tokens = pack[0], pack[1], pack[2]
        entry = row(_agent_label(consumer))
        entry["packs"] += 1
        if tokens is not None:
            carried = (pack[4] or 0) + (pack[5] or 0) + (pack[6] or 0) > 0
            entry["packs_with_memory"] = (entry["packs_with_memory"] or 0) + (1 if carried else 0)
        seen = _iso(created_at)
        if seen and (entry["last_seen_at"] is None or seen > entry["last_seen_at"]):
            entry["last_seen_at"] = seen

    rows = []
    for entry in agents.values():
        entry["surfaces"] = sorted(entry.pop("surfaces"))
        entry["success_rate"] = _share(entry["successes"], entry["requests"])
        entry["pack_rate"] = _share(entry["packs"], entry["context_requests"])
        rows.append(entry)
    # Busiest first, and the unattributed bucket last whatever its size: it is
    # a gap in attribution, not an agent anyone can act on.
    rows.sort(key=lambda r: (r["agent"] == "unattributed", -(r["requests"] + r["packs"]), r["agent"]))
    return rows


def _rule_section(events: Sequence[Any], measurement_started: Optional[datetime], active: int) -> dict[str, Any]:
    """Reach and efficacy per rule, plus how many rules nothing has used."""
    per_rule: dict[str, dict[str, int]] = {}
    for rule_id, kind, _created_at in events:
        entry = per_rule.setdefault(rule_id, {"delivered": 0, "flagged": 0})
        if kind == DELIVERED:
            entry["delivered"] += 1
        elif kind == FLAGGED:
            entry["flagged"] += 1

    delivered_rules = sum(1 for e in per_rule.values() if e["delivered"])
    flagged_rules = sum(1 for e in per_rule.values() if e["flagged"])
    started = _utc(measurement_started) if measurement_started else None
    top = sorted(
        ({"rule_id": rule_id, **counts} for rule_id, counts in per_rule.items()),
        key=lambda r: (-r["flagged"], -r["delivered"], r["rule_id"]),
    )[:10]
    return {
        "active": active,
        "measurement_started_at": _iso(started),
        "delivered_events": sum(e["delivered"] for e in per_rule.values()),
        "flagged_events": sum(e["flagged"] for e in per_rule.values()),
        "delivered_rules": delivered_rules if started else None,
        "flagged_rules": flagged_rules if started else None,
        # Only answerable once something has been recorded. Before that, every
        # rule would look unused because nothing was watching.
        "unused": max(active - delivered_rules, 0) if started else None,
        "top": top,
    }


async def rule_usage_counts(
    rule_ids: Sequence[str], *, period_days: int = 30, repository_id: Optional[int] = None
) -> dict[str, Any]:
    """Per-rule ``delivered``/``flagged`` counts for a page of rules.

    Returns ``{"measurement_started_at": iso|None, "counts": {rule_id: {...}}}``.
    A rule missing from ``counts`` was genuinely not used in the window; a null
    ``measurement_started_at`` means nothing was being counted yet, and the
    caller must not render zeros in that case.
    """
    ids = [str(r) for r in rule_ids if r]
    if not ids:
        return {"measurement_started_at": None, "counts": {}}
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=max(1, period_days))
    filters = [
        RuleUsageEvent.rule_id.in_(ids),
        RuleUsageEvent.created_at >= start,
        RuleUsageEvent.created_at <= now,
    ]
    if repository_id is not None:
        filters.append(RuleUsageEvent.repository_id == repository_id)
    async with async_session_factory() as session:
        rows = (
            await session.execute(
                select(RuleUsageEvent.rule_id, RuleUsageEvent.kind, func.count())
                .where(*filters)
                .group_by(RuleUsageEvent.rule_id, RuleUsageEvent.kind)
            )
        ).all()
        started = (await session.execute(select(func.min(RuleUsageEvent.created_at)))).scalar_one_or_none()
    counts: dict[str, dict[str, int]] = {}
    for rule_id, kind, total in rows:
        entry = counts.setdefault(rule_id, {"delivered": 0, "flagged": 0})
        if kind == DELIVERED:
            entry["delivered"] = int(total)
        elif kind == FLAGGED:
            entry["flagged"] = int(total)
    return {"measurement_started_at": _iso(started), "counts": counts}
