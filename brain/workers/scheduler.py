"""In-process scheduler: fires the recurring jobs from inside the worker.

Exactly-once per slot: every worker (and every restart) evaluates the same
slots, and a Redis ``SET NX`` lock keyed by job type + slot time lets only one
of them enqueue. A slot missed while no worker was running fires once on the
next tick if it is within ``SCHEDULER_CATCHUP_WINDOW_S``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from loguru import logger
from redis.asyncio import Redis

from brain.config.settings import settings
from brain.workers.queue import queue_for_job, worker_pool_for_job

_LOOKAHEAD_MINUTES = 8 * 24 * 60


@dataclass(frozen=True)
class ScheduledJob:
    job_type: str
    cron: str
    description: str
    params: Callable[[datetime], Dict[str, Any]] = field(default=lambda _slot: {})
    timeout_seconds: Optional[int] = None


def _repo_path() -> str:
    return settings.PROJECT_BRAIN_GIT_REPO_PATH


# Same cron times (UTC) and payloads as the retired n8n workflows.
SCHEDULES: tuple[ScheduledJob, ...] = (
    ScheduledJob(
        "nightly_maintenance",
        "30 0 * * *",
        "Nightly deep maintenance",
        lambda _slot: {"repo_path": _repo_path(), "scheduled": True, "notify": True},
        timeout_seconds=settings.WORKER_MAX_JOB_TIMEOUT_SECONDS,
    ),
    ScheduledJob("health_check", "0 2 * * *", "Nightly harness health"),
    ScheduledJob(
        "self_diagnosis",
        "30 3 * * *",
        "Nightly LLM self-diagnosis",
        lambda slot: {
            "scheduled": True,
            "use_llm": True,
            "trigger_context": {"source": "scheduler", "reference": f"self_diagnosis:{slot.isoformat()}"},
        },
    ),
    ScheduledJob(
        "benchmark",
        "0 6 * * 1",
        "Weekly smoke benchmark",
        lambda _slot: {"golden": "rules/golden_tasks.yaml", "smoke": True},
    ),
)
SCHEDULES_BY_TYPE = {job.job_type: job for job in SCHEDULES}
MEMORY_SCHEDULE = ScheduledJob("memory_consolidation", "15 * * * *", "Hourly memory consolidation",
                               lambda _slot: {"scheduled": True})
SCHEDULES_BY_TYPE[MEMORY_SCHEDULE.job_type] = MEMORY_SCHEDULE


def active_schedules() -> tuple[ScheduledJob, ...]:
    # Independent of optional deep maintenance; disabled consolidation adds no
    # hourly queue noise or misleading stale-job warning.
    return SCHEDULES + ((MEMORY_SCHEDULE,) if settings.MEMORY_CONSOLIDATION_ENABLED else ())


# --------------------------------------------------------------------------- cron


def _parse_field(spec: str, lo: int, hi: int) -> frozenset[int]:
    values: set[int] = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            step = int(step_s)
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = end = int(part)
        if start < lo or end > hi or start > end or step < 1:
            raise ValueError(f"cron field {spec!r} out of range {lo}-{hi}")
        values.update(range(start, end + 1, step))
    return frozenset(values)


@dataclass(frozen=True)
class Cron:
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # cron numbering: 0 = Sunday

    @classmethod
    def parse(cls, expr: str) -> "Cron":
        parts = expr.split()
        if len(parts) != 5:
            raise ValueError(f"cron expression needs 5 fields: {expr!r}")
        m, h, dom, mon, dow = parts
        return cls(
            _parse_field(m, 0, 59),
            _parse_field(h, 0, 23),
            _parse_field(dom, 1, 31),
            _parse_field(mon, 1, 12),
            frozenset(d % 7 for d in _parse_field(dow, 0, 7)),
        )

    def matches(self, t: datetime) -> bool:
        return (
            t.minute in self.minutes
            and t.hour in self.hours
            and t.day in self.days
            and t.month in self.months
            and (t.weekday() + 1) % 7 in self.weekdays
        )

    def latest(self, now: datetime) -> Optional[datetime]:
        """Most recent slot at or before ``now`` (minute resolution, UTC)."""
        t = now.astimezone(timezone.utc).replace(second=0, microsecond=0)
        for _ in range(_LOOKAHEAD_MINUTES):
            if self.matches(t):
                return t
            t -= timedelta(minutes=1)
        return None

    def next_after(self, now: datetime) -> Optional[datetime]:
        t = now.astimezone(timezone.utc).replace(second=0, microsecond=0) + timedelta(minutes=1)
        for _ in range(_LOOKAHEAD_MINUTES):
            if self.matches(t):
                return t
            t += timedelta(minutes=1)
        return None

    def interval_seconds(self, now: datetime) -> int:
        first = self.next_after(now)
        second = self.next_after(first) if first else None
        return int((second - first).total_seconds()) if first and second else 7 * 24 * 3600


# --------------------------------------------------------------------------- keys


def _prefix() -> str:
    return settings.WORKER_REDIS_PREFIX


def slot_lock_key(job_type: str, slot: datetime) -> str:
    return f"{_prefix()}:scheduler:slot:{job_type}:{slot.strftime('%Y-%m-%dT%H:%MZ')}"


def job_stats_key(job_type: str) -> str:
    return f"{_prefix()}:jobstats:{job_type}"


def started_at_key() -> str:
    return f"{_prefix()}:scheduler:started_at"


def _s(value: Any) -> Optional[str]:
    if value is None:
        return None
    return value.decode() if isinstance(value, bytes) else str(value)


def _dt(value: Any) -> Optional[datetime]:
    raw = _s(value)
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- tick


async def tick(redis: Redis, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Enqueue every due slot this process wins the lock for. Returns what was fired."""
    now = now or datetime.now(timezone.utc)
    window = max(int(settings.SCHEDULER_CATCHUP_WINDOW_S), 2 * int(settings.SCHEDULER_TICK_SECONDS))
    fired: List[Dict[str, Any]] = []
    for job in active_schedules():
        cron = Cron.parse(job.cron)
        slot = cron.latest(now)
        if slot is None or (now - slot).total_seconds() > window:
            continue
        key = slot_lock_key(job.job_type, slot)
        ttl = max(cron.interval_seconds(now) * 2, window + 3600)
        if not await redis.set(key, now.isoformat(), nx=True, ex=ttl):
            continue
        try:
            queue = queue_for_job(
                redis, _prefix(), job.job_type, pools_enabled=settings.BRAIN_WORKER_POOLS_V2_ENABLED
            )
            job_id = await queue.enqueue(
                job.job_type,
                job.params(slot),
                idempotency_key=f"scheduler:{job.job_type}:{slot.isoformat()}",
                max_attempts=max(1, int(settings.SCHEDULER_JOB_MAX_ATTEMPTS)),
                timeout_seconds=job.timeout_seconds,
            )
        except Exception as exc:
            # Give the slot back so the next tick (or another worker) retries it.
            logger.warning("Scheduler enqueue failed for {} @ {}: {}", job.job_type, slot, exc)
            await redis.delete(key)
            continue
        await redis.hset(
            job_stats_key(job.job_type),
            mapping={"last_scheduled_at": now.isoformat(), "last_slot": slot.isoformat(), "last_job_id": job_id},
        )
        late = int((now - slot).total_seconds())
        logger.info("Scheduler fired {} for slot {} ({}s late) as job {}", job.job_type, slot, late, job_id)
        fired.append({"job_type": job.job_type, "slot": slot.isoformat(), "job_id": job_id, "catch_up": late > 120})
    return fired


async def run_scheduler(redis: Redis, stop: asyncio.Event) -> None:
    if not settings.SCHEDULER_ENABLED:
        logger.info("Scheduler disabled (SCHEDULER_ENABLED=false)")
        return
    await redis.set(started_at_key(), datetime.now(timezone.utc).isoformat(), nx=True)
    logger.info("Scheduler started: {}", ", ".join(f"{j.job_type}@'{j.cron}'" for j in active_schedules()))
    interval = max(5, int(settings.SCHEDULER_TICK_SECONDS))
    while not stop.is_set():
        try:
            await tick(redis)
        except Exception as exc:
            logger.warning("Scheduler tick failed: {}: {}", type(exc).__name__, exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue


# --------------------------------------------------------------------------- status


def _job_status(
    *, now: datetime, interval: int, last_success: Optional[datetime], last_failure: Optional[datetime],
    started: Optional[datetime],
) -> str:
    if not settings.SCHEDULER_ENABLED:
        return "disabled"
    deadline = interval + int(settings.SCHEDULER_STALE_GRACE_S)
    reference = last_success or started
    if reference is None:
        return "pending"
    if (now - reference).total_seconds() > deadline:
        return "stale"
    if last_failure and (last_success is None or last_failure > last_success):
        return "degraded"
    return "ok" if last_success else "pending"


async def scheduler_status(redis: Redis, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Per-job schedule state for /health and /scheduler/jobs."""
    now = now or datetime.now(timezone.utc)
    started = _dt(await redis.get(started_at_key()))
    jobs: List[Dict[str, Any]] = []
    for job in active_schedules():
        cron = Cron.parse(job.cron)
        raw = {(_s(k) or ""): v for k, v in (await redis.hgetall(job_stats_key(job.job_type)) or {}).items()}
        last_success, last_failure = _dt(raw.get("last_success_at")), _dt(raw.get("last_failure_at"))
        interval = cron.interval_seconds(now)
        next_run = cron.next_after(now)
        jobs.append(
            {
                "job_type": job.job_type,
                "description": job.description,
                "cron": job.cron,
                "pool": worker_pool_for_job(job.job_type),
                "interval_seconds": interval,
                "next_run_at": next_run.isoformat() if next_run else None,
                "last_scheduled_at": _s(raw.get("last_scheduled_at")),
                "last_job_id": _s(raw.get("last_job_id")),
                "last_success_at": last_success.isoformat() if last_success else None,
                "last_failure_at": last_failure.isoformat() if last_failure else None,
                "last_error": _s(raw.get("last_error")),
                "last_attempts": int(_s(raw.get("last_attempts")) or 0) or None,
                "max_attempts": max(1, int(settings.SCHEDULER_JOB_MAX_ATTEMPTS)),
                "deadman_configured": bool(deadman_url(job.job_type)),
                "status": _job_status(
                    now=now, interval=interval, last_success=last_success, last_failure=last_failure, started=started
                ),
            }
        )
    stale = [j["job_type"] for j in jobs if j["status"] == "stale"]
    return {
        "enabled": bool(settings.SCHEDULER_ENABLED),
        "started_at": started.isoformat() if started else None,
        "grace_seconds": int(settings.SCHEDULER_STALE_GRACE_S),
        "stale": stale,
        "jobs": jobs,
    }


def deadman_url(job_type: str) -> Optional[str]:
    value = getattr(settings, f"DEADMAN_URL_{job_type.upper()}", None)
    return value.strip() if isinstance(value, str) and value.strip() else None
