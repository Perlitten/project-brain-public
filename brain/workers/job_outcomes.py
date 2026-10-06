"""Per-job-type outcome ledger, dead-man pings, and failure → self-diagnosis routing."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

import httpx
from loguru import logger
from redis.asyncio import Redis

from brain.config.settings import settings
from brain.workers.queue import queue_for_job
from brain.workers.scheduler import deadman_url, job_stats_key


def retry_delay_seconds(attempt: int) -> int:
    """Exponential backoff for attempt n (1-based): base·2^(n-1), capped."""
    base = max(1, int(settings.WORKER_RETRY_BASE_DELAY_S))
    return int(min(base * (2 ** max(0, attempt - 1)), max(base, int(settings.WORKER_RETRY_MAX_DELAY_S))))


async def record_success(redis: Redis, job_type: str, job_id: str, attempt: int) -> None:
    try:
        await redis.hset(
            job_stats_key(job_type),
            mapping={
                "last_success_at": datetime.now(timezone.utc).isoformat(),
                "last_success_job_id": job_id,
                "last_attempts": str(attempt),
            },
        )
    except Exception as exc:
        logger.warning("Could not record success for {}: {}", job_type, exc)
    await ping_deadman(job_type)


async def record_failure(redis: Redis, job_type: str, job_id: str, attempt: int, error: str) -> None:
    try:
        await redis.hset(
            job_stats_key(job_type),
            mapping={
                "last_failure_at": datetime.now(timezone.utc).isoformat(),
                "last_failure_job_id": job_id,
                "last_error": error[:1000],
                "last_attempts": str(attempt),
            },
        )
    except Exception as exc:
        logger.warning("Could not record failure for {}: {}", job_type, exc)


async def ping_deadman(job_type: str) -> bool:
    url = deadman_url(job_type)
    if not url:
        return False
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url)
        return response.status_code < 400
    except Exception as exc:
        logger.warning("Dead-man ping for {} failed: {}", job_type, type(exc).__name__)
        return False


async def route_final_failure(
    redis: Redis, job_type: str, job_id: str, error: str, *, now: Optional[float] = None
) -> Optional[str]:
    """Enqueue self_diagnosis for a job that exhausted its attempts (never for self_diagnosis itself)."""
    if job_type == "self_diagnosis":
        return None
    bucket = int((now if now is not None else time.time()) // 300)
    try:
        queue = queue_for_job(
            redis, settings.WORKER_REDIS_PREFIX, "self_diagnosis", pools_enabled=settings.BRAIN_WORKER_POOLS_V2_ENABLED
        )
        return await queue.enqueue(
            "self_diagnosis",
            {
                "use_llm": True,
                "trigger_context": {
                    "source": "job_failure",
                    "summary": error[:1000] or f"{job_type} failed",
                    "reference": f"{job_type}:{job_id}"[:255],
                },
            },
            idempotency_key=f"job-failure:{job_type}:{bucket}",
            max_attempts=max(1, int(settings.SELF_DIAGNOSIS_JOB_MAX_ATTEMPTS)),
        )
    except Exception as exc:
        logger.warning("Could not route {} failure to self-diagnosis: {}", job_type, exc)
        return None
