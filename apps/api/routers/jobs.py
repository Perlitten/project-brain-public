from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException

from apps.api.auth import require_api_key, require_scope
from apps.api.schemas import (
    BenchmarkJobRequest,
    DeepContextJobRequest,
    EmbeddingBackfillRequest,
    EmbeddingVerifyRequest,
    MemoryConsolidationJobRequest,
    NightlyMaintenanceJobRequest,
    ProactiveInsightsJobRequest,
    ReindexJobRequest,
    SelfDiagnosisJobRequest,
)
from brain.database.session import redis_client
from brain.config.settings import settings
from brain.workers.queue import JobQueue, WORKER_POOLS, queue_for_job, worker_pool_prefix

router = APIRouter(prefix="/jobs", tags=["jobs"], dependencies=[Depends(require_api_key)])


def _queue(job_type: str | None = None) -> JobQueue:
    if not job_type or not settings.BRAIN_WORKER_POOLS_V2_ENABLED:
        return JobQueue(redis_client, prefix=settings.WORKER_REDIS_PREFIX)
    return queue_for_job(
        redis_client,
        settings.WORKER_REDIS_PREFIX,
        job_type,
        pools_enabled=settings.BRAIN_WORKER_POOLS_V2_ENABLED,
    )


def _enqueue_response(job_id: str) -> dict:
    return {
        "job_id": job_id,
        "status": "queued",
        "status_url": f"/jobs/{job_id}",
    }


@router.post("/reindex", dependencies=[Depends(require_scope("jobs:write"))])
async def enqueue_reindex(body: ReindexJobRequest | None = None):
    params: dict[str, Any] = {}
    if body:
        if body.repo_path:
            params["repo_path"] = body.repo_path
        params["clean"] = body.clean
        if body.source_revision:
            params["source_revision"] = body.source_revision
        params["verify_after"] = body.verify_after
        params["benchmark_after"] = body.benchmark_after
    job_id = await _queue("reindex").enqueue(
        "reindex",
        params,
        idempotency_key=body.idempotency_key if body else None,
        max_attempts=3,
    )
    return _enqueue_response(job_id)


@router.post("/health-check", dependencies=[Depends(require_scope("jobs:write"))])
async def enqueue_health_check(repo_path: Optional[str] = None):
    params = {"repo_path": repo_path} if repo_path else None
    job_id = await _queue("health_check").enqueue("health_check", params if params else None)
    return _enqueue_response(job_id)


@router.post("/embedding-verify", dependencies=[Depends(require_scope("jobs:write"))])
async def enqueue_embedding_verify(body: EmbeddingVerifyRequest | None = None):
    params: dict[str, Any] = {}
    if body and body.repo_path:
        params["repo_path"] = body.repo_path
    job_id = await _queue("embedding_verify").enqueue("embedding_verify", params or None)
    return _enqueue_response(job_id)


@router.post("/embedding-backfill", dependencies=[Depends(require_scope("jobs:write"))])
async def enqueue_embedding_backfill(body: EmbeddingBackfillRequest | None = None):
    params: dict[str, Any] = {}
    if body:
        if body.repo_path:
            params["repo_path"] = body.repo_path
        if body.pgvector_only:
            params["pgvector_only"] = body.pgvector_only
        if body.limit is not None:
            params["limit"] = body.limit
        params["batch_size"] = body.batch_size
        if body.provider:
            params["provider"] = body.provider
    job_id = await _queue("embedding_backfill").enqueue("embedding_backfill", params or None)
    return _enqueue_response(job_id)


@router.post("/benchmark", dependencies=[Depends(require_scope("jobs:write"))])
async def enqueue_benchmark(body: BenchmarkJobRequest | None = None):
    params: dict[str, Any] = {}
    if body:
        if body.repo_path:
            params["repo_path"] = body.repo_path
        if body.golden:
            params["golden"] = body.golden
        if body.smoke:
            params["smoke"] = True
    job_id = await _queue("benchmark").enqueue("benchmark", params or None)
    return _enqueue_response(job_id)


@router.post("/proactive-insights", dependencies=[Depends(require_scope("jobs:write"))])
async def enqueue_proactive_insights(body: ProactiveInsightsJobRequest | None = None):
    params: dict[str, Any] = {}
    if body:
        if body.repo_path:
            params["repo_path"] = body.repo_path
        if body.use_llm is not None:
            params["use_llm"] = body.use_llm
        if body.scheduled:
            params["scheduled"] = True
    job_id = await _queue("proactive_insights").enqueue("proactive_insights", params or None)
    return _enqueue_response(job_id)


@router.post("/memory-consolidation", dependencies=[Depends(require_scope("jobs:write"))])
async def enqueue_memory_consolidation(body: MemoryConsolidationJobRequest | None = None):
    params: dict[str, Any] = {}
    if body:
        if body.scheduled:
            params["scheduled"] = True
        if body.require_approval is not None:
            params["require_approval"] = body.require_approval
        if body.dry_run:
            params["dry_run"] = True
    job_id = await _queue("memory_consolidation").enqueue("memory_consolidation", params or None)
    return _enqueue_response(job_id)


@router.post("/self-diagnosis", dependencies=[Depends(require_scope("jobs:write"))])
async def enqueue_self_diagnosis(body: SelfDiagnosisJobRequest | None = None):
    params: dict[str, Any] = {}
    if body:
        if body.repo_path:
            params["repo_path"] = body.repo_path
        if body.use_llm is not None:
            params["use_llm"] = body.use_llm
        if body.scheduled:
            params["scheduled"] = True
        trigger_context = {
            "source": body.trigger_source,
            "summary": body.trigger_summary,
            "reference": body.trigger_reference,
        }
        if any(trigger_context.values()):
            params["trigger_context"] = {key: value for key, value in trigger_context.items() if value is not None}
    idempotency_key = body.idempotency_key if body else None
    job_id = await _queue("self_diagnosis").enqueue(
        "self_diagnosis",
        params or None,
        idempotency_key=idempotency_key,
        max_attempts=settings.SELF_DIAGNOSIS_JOB_MAX_ATTEMPTS,
    )
    return _enqueue_response(job_id)


@router.post("/nightly-maintenance", dependencies=[Depends(require_scope("jobs:write"))])
async def enqueue_nightly_maintenance(
    body: NightlyMaintenanceJobRequest | None = None,
):
    params: dict[str, Any] = {}
    if body:
        if body.repo_path:
            params["repo_path"] = body.repo_path
        if body.scheduled:
            params["scheduled"] = True
        params["notify"] = body.notify
    job_id = await _queue("nightly_maintenance").enqueue(
        "nightly_maintenance",
        params or None,
        idempotency_key=body.idempotency_key if body else None,
        max_attempts=2,
        timeout_seconds=settings.WORKER_MAX_JOB_TIMEOUT_SECONDS,
    )
    return _enqueue_response(job_id)


@router.post("/deep-context", dependencies=[Depends(require_scope("jobs:write"))])
async def enqueue_deep_context(body: DeepContextJobRequest):
    if not settings.LATE_INTERACTION_DEEP_ENABLED:
        raise HTTPException(
            status_code=503,
            detail="LFM deep lane is disabled",
        )
    params: dict[str, Any] = {
        "task_description": body.task_description,
        "notify": body.notify,
    }
    if body.repo_path:
        params["repo_path"] = body.repo_path
    job_id = await _queue("deep_context").enqueue(
        "deep_context",
        params,
        idempotency_key=body.idempotency_key,
        max_attempts=1,
        timeout_seconds=settings.WORKER_MAX_JOB_TIMEOUT_SECONDS,
    )
    return _enqueue_response(job_id)


@router.get("/{job_id}", dependencies=[Depends(require_scope("jobs:read"))])
async def get_job_status(job_id: str, include_result: bool = False):
    queues = [_queue()]
    if settings.BRAIN_WORKER_POOLS_V2_ENABLED:
        queues = [
            JobQueue(
                redis_client,
                prefix=worker_pool_prefix(settings.WORKER_REDIS_PREFIX, pool, enabled=True),
            )
            for pool in WORKER_POOLS
        ]
        # Existing jobs are drained before cutover, but retaining this read-only
        # fallback keeps their status observable during the transition.
        queues.append(_queue())
    job = None
    for queue in queues:
        job = await queue.get_job(job_id)
        if job:
            job["pool"] = queue.prefix.rsplit(":", 1)[-1] if settings.BRAIN_WORKER_POOLS_V2_ENABLED else "legacy"
            break
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not include_result:
        result = job.pop("result", None)
        # Queued jobs are initialized with an empty result. Its presence is not
        # evidence that a caller can fetch a result yet.
        job["result_available"] = result not in (None, "", {}, [])
    return job
