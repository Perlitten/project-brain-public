"""Job dispatch with lease-fencing validation."""
from __future__ import annotations

from typing import Any, Dict, Optional
from brain.config.settings import settings
from brain.database.session import redis_client
from brain.workers.git_connect import run_git_connect
from brain.workers.origin_poll import run_origin_poll
from brain.workers.db_fencing import DbFence, check_db_fence_open, current_job_fence
from brain.workers.errors import StaleWorkerFencingError
from .jobs import (run_benchmark, run_deep_context, run_diff_review, run_embedding_backfill, run_embedding_verify, run_enrich_summaries, run_health_check, run_memory_consolidation, run_nightly_maintenance, run_proactive_insights, run_reindex, run_self_diagnosis, run_team_run_execution)


async def validate_task_fencing(redis_cli, prefix: str, job_id: str, fencing_token: Optional[str]) -> None:
    if not fencing_token:
        return
    lease_key = f"{prefix}:job:{job_id}:lease"
    active_token = await redis_cli.hget(lease_key, "fencing_token")
    if isinstance(active_token, bytes):
        active_token = active_token.decode()
    if active_token != fencing_token:
        raise StaleWorkerFencingError(
            f"Fencing mismatch for job {job_id}: active_token={active_token}, worker_token={fencing_token}. "
            "Job lease was claimed by another worker; aborting database mutation."
        )


async def execute_job(
    job_type: str,
    params: Optional[Dict[str, Any]] = None,
    *,
    job_id: Optional[str] = None,
    fencing_token: Optional[str] = None,
    lease_prefix: Optional[str] = None,
) -> Dict[str, Any]:
    """Dispatch a job to its handler, enforcing lease ownership.

    When the worker passes ``job_id`` + ``fencing_token``, ownership is
    verified before dispatch (Redis fast path) and again — durably, in
    Postgres via ``check_db_fence_open`` — before the result is returned, so
    a worker whose lease was claimed by another cannot commit its outcome.
    ``current_job_fence`` also carries the fence to any handler-internal
    transaction that opts into ``assert_current_db_fence``.
    """
    fence_token_ctx = None
    if fencing_token and job_id:
        await validate_task_fencing(
            redis_client, lease_prefix or settings.WORKER_REDIS_PREFIX, job_id, fencing_token
        )
        fence_token_ctx = current_job_fence.set(
            DbFence(job_id=job_id, fencing_token=fencing_token)
        )
    params = params or {}
    handlers = {
        "enrich_summaries": run_enrich_summaries,
        "reindex": run_reindex,
        "diff_review": run_diff_review,
        "health_check": run_health_check,
        "embedding_verify": run_embedding_verify,
        "embedding_backfill": run_embedding_backfill,
        "benchmark": run_benchmark,
        "proactive_insights": run_proactive_insights,
        "memory_consolidation": run_memory_consolidation,
        "self_diagnosis": run_self_diagnosis,
        "origin_poll": run_origin_poll,
        "git_connect": run_git_connect,
        "nightly_maintenance": run_nightly_maintenance,
        "deep_context": run_deep_context,
        "team_run_execution": run_team_run_execution,
    }
    handler = handlers.get(job_type)
    if handler is None:
        if fence_token_ctx is not None:
            current_job_fence.reset(fence_token_ctx)
        raise ValueError(f"Unknown job type: {job_type}")
    try:
        result = await handler(params)
    finally:
        if fence_token_ctx is not None:
            current_job_fence.reset(fence_token_ctx)
    if fencing_token and job_id:
        # Durable check in Postgres — a Redis read alone cannot protect the
        # result/status commit that follows this return.
        await check_db_fence_open(job_id=job_id, fencing_token=fencing_token)
    return result
