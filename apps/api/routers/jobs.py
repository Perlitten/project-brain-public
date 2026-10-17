from typing import Any, Optional, cast

from fastapi import APIRouter, Depends, HTTPException, Request

from apps.api.auth import require_api_key, require_principal, require_scope
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
from apps.api.team_access import repository_authority
from brain.auth.principals import ResolvedPrincipal
from brain.database.session import redis_client
from brain.config.settings import settings
from brain.memory.freshness_envelope import attach_envelope, envelope_for_path
from brain.team.authorization import Permission
from brain.workers.initiator import JobInitiator
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


async def _authorize_job(principal: ResolvedPrincipal, repo_path: str | None) -> None:
    # A job without a repository touches every repository: a team administrator once members exist.
    await repository_authority(principal, Permission.REQUEST_RUN, repo_path=repo_path)


def _initiator(principal: ResolvedPrincipal) -> JobInitiator:
    """Recorded on the job so the worker re-authorizes the same caller and permission before it runs."""
    return JobInitiator.for_principal(principal, Permission.REQUEST_RUN)


def _enqueue_response(job_id: str) -> dict:
    return {
        "job_id": job_id,
        "status": "queued",
        "status_url": f"/jobs/{job_id}",
    }


@router.post("/reindex", dependencies=[Depends(require_scope("jobs:write"))])
async def enqueue_reindex(body: ReindexJobRequest | None = None, principal: ResolvedPrincipal = Depends(require_principal)):
    await _authorize_job(principal, getattr(body, "repo_path", None))
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
        initiator=_initiator(principal),
    )
    return _enqueue_response(job_id)


@router.post("/health-check", dependencies=[Depends(require_scope("jobs:write"))])
async def enqueue_health_check(repo_path: Optional[str] = None, principal: ResolvedPrincipal = Depends(require_principal)):
    await _authorize_job(principal, repo_path)
    params = {"repo_path": repo_path} if repo_path else None
    job_id = await _queue("health_check").enqueue(
        "health_check", params if params else None,
        initiator=_initiator(principal),
    )
    return _enqueue_response(job_id)


@router.post("/embedding-verify", dependencies=[Depends(require_scope("jobs:write"))])
async def enqueue_embedding_verify(body: EmbeddingVerifyRequest | None = None, principal: ResolvedPrincipal = Depends(require_principal)):
    await _authorize_job(principal, getattr(body, "repo_path", None))
    params: dict[str, Any] = {}
    if body and body.repo_path:
        params["repo_path"] = body.repo_path
    job_id = await _queue("embedding_verify").enqueue(
        "embedding_verify", params or None,
        initiator=_initiator(principal),
    )
    return _enqueue_response(job_id)


@router.post("/embedding-backfill", dependencies=[Depends(require_scope("jobs:write"))])
async def enqueue_embedding_backfill(body: EmbeddingBackfillRequest | None = None, principal: ResolvedPrincipal = Depends(require_principal)):
    await _authorize_job(principal, getattr(body, "repo_path", None))
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
    job_id = await _queue("embedding_backfill").enqueue(
        "embedding_backfill", params or None,
        initiator=_initiator(principal),
    )
    return _enqueue_response(job_id)


@router.post("/benchmark", dependencies=[Depends(require_scope("jobs:write"))])
async def enqueue_benchmark(body: BenchmarkJobRequest | None = None, principal: ResolvedPrincipal = Depends(require_principal)):
    await _authorize_job(principal, getattr(body, "repo_path", None))
    params: dict[str, Any] = {}
    if body:
        if body.repo_path:
            params["repo_path"] = body.repo_path
        if body.golden:
            params["golden"] = body.golden
        if body.smoke:
            params["smoke"] = True
    job_id = await _queue("benchmark").enqueue("benchmark", params or None, initiator=_initiator(principal))
    return _enqueue_response(job_id)


@router.post("/proactive-insights", dependencies=[Depends(require_scope("jobs:write"))])
async def enqueue_proactive_insights(body: ProactiveInsightsJobRequest | None = None, principal: ResolvedPrincipal = Depends(require_principal)):
    await _authorize_job(principal, getattr(body, "repo_path", None))
    params: dict[str, Any] = {}
    if body:
        if body.repo_path:
            params["repo_path"] = body.repo_path
        if body.use_llm is not None:
            params["use_llm"] = body.use_llm
        if body.scheduled:
            params["scheduled"] = True
    job_id = await _queue("proactive_insights").enqueue(
        "proactive_insights", params or None,
        initiator=_initiator(principal),
    )
    return _enqueue_response(job_id)


@router.post("/memory-consolidation", dependencies=[Depends(require_scope("jobs:write"))])
async def enqueue_memory_consolidation(body: MemoryConsolidationJobRequest | None = None, principal: ResolvedPrincipal = Depends(require_principal)):
    await _authorize_job(principal, getattr(body, "repo_path", None))
    params: dict[str, Any] = {}
    if body:
        if body.scheduled:
            params["scheduled"] = True
        if body.require_approval is not None:
            params["require_approval"] = body.require_approval
        if body.dry_run:
            params["dry_run"] = True
    job_id = await _queue("memory_consolidation").enqueue(
        "memory_consolidation", params or None,
        initiator=_initiator(principal),
    )
    return _enqueue_response(job_id)


@router.post("/self-diagnosis", dependencies=[Depends(require_scope("jobs:write"))])
async def enqueue_self_diagnosis(body: SelfDiagnosisJobRequest | None = None, principal: ResolvedPrincipal = Depends(require_principal)):
    await _authorize_job(principal, getattr(body, "repo_path", None))
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
        initiator=_initiator(principal),
    )
    return _enqueue_response(job_id)


@router.post("/nightly-maintenance", dependencies=[Depends(require_scope("jobs:write"))])
async def enqueue_nightly_maintenance(
    body: NightlyMaintenanceJobRequest | None = None,
    principal: ResolvedPrincipal = Depends(require_principal),
):
    await _authorize_job(principal, getattr(body, "repo_path", None))
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
        initiator=_initiator(principal),
    )
    return _enqueue_response(job_id)


@router.post("/deep-context", dependencies=[Depends(require_scope("jobs:write"))])
async def enqueue_deep_context(body: DeepContextJobRequest, request: Request = cast(Request, None),
                               principal: ResolvedPrincipal = Depends(require_principal)):
    await _authorize_job(principal, body.repo_path)
    # Without the late-interaction lane the job still builds a durable pack
    # with the default retrieval (see brain.workers.tasks.run_deep_context).
    from apps.api.request_observation import requesting_principal

    params: dict[str, Any] = {
        "task_description": body.task_description,
        "notify": body.notify,
        # Carried into the job because the worker that builds the pack cannot
        # see who asked, and an unattributed pack counts towards no agent.
        "consumer": requesting_principal(request),
    }
    if body.repo_path:
        params["repo_path"] = body.repo_path
    job_id = await _queue("deep_context").enqueue(
        "deep_context",
        params,
        idempotency_key=body.idempotency_key,
        max_attempts=1,
        timeout_seconds=settings.WORKER_MAX_JOB_TIMEOUT_SECONDS,
        initiator=_initiator(principal),
    )
    return attach_envelope(
        _enqueue_response(job_id), await envelope_for_path(body.repo_path or settings.TARGET_REPO_PATH)
    )


@router.get("/{job_id}", dependencies=[Depends(require_scope("jobs:read"))])
async def get_job_status(job_id: str, include_result: bool = False,
                         principal: ResolvedPrincipal = Depends(require_principal)):
    """Status of one queued job; the caller needs ``read`` on the job's repository.

    A job the caller may not read is the same 404 as a missing one, so its
    existence is not revealed; a job without a repository needs a team administrator."""
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
    params = job.get("params") or {}
    repository_id = params.get("repository_id")
    try:
        if "repository_id" in params:
            await repository_authority(principal, Permission.READ,
                                       repository_id=repository_id if isinstance(repository_id, int) else None)
        else:
            await repository_authority(principal, Permission.READ, repo_path=params.get("repo_path"))
    except HTTPException as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    if not include_result:
        result = job.pop("result", None)
        # Queued jobs are initialized with an empty result. Its presence is not
        # evidence that a caller can fetch a result yet.
        job["result_available"] = result not in (None, "", {}, [])
    return job
