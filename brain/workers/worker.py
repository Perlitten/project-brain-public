"""Redis worker process — consumes brain:worker queue."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from datetime import datetime, timezone

from loguru import logger

from brain.config.settings import settings
from brain.database.session import close_database_connections, init_db, redis_client
from brain.late_interaction.client import (
    LateInteractionReleaseUnavailableError,
    close_late_interaction_client,
    validate_remote_late_interaction_release,
)
from brain.workers.db_fencing import (
    release_db_lease_open,
    renew_db_lease_open,
    try_claim_db_lease,
)
from brain.workers.queue import JobQueue, JobStatus, worker_pool_prefix, queue_for_job
from brain.workers.errors import PermanentJobError
from brain.workers.tasks import StaleWorkerFencingError, execute_job, validate_task_fencing
from brain.workers.runtime import JobRuntime, current_job
from brain.version import build_info


async def _finish_status(queue: JobQueue, job_id: str, fencing_token: str | None, status: JobStatus, **kwargs) -> None:
    """Terminal/transition writes bound to lease ownership: a stale worker's
    fenced write is rejected and surfaced as an ownership loss, never applied."""
    if fencing_token:
        written = await queue.update_status(job_id, status, fencing_token=fencing_token, **kwargs)
        if written is False:
            raise StaleWorkerFencingError(
                f"Job {job_id} lease claimed by another worker; {status.value} write rejected"
            )
    else:
        await queue.update_status(job_id, status, **kwargs)


async def _execute_owned_job(
    queue: JobQueue,
    job_id: str,
    fencing_token: str,
    job_type: str,
    params: dict,
    lease_ttl: int,
) -> dict:
    """Run a job while holding its lease.

    A background renewal task extends the lease at ttl/3 intervals; when a
    renewal is refused (another worker claimed the job), the execution task is
    cancelled and StaleWorkerFencingError is raised so no terminal state or
    durable side effect is committed by this worker.
    """
    renew_stop = asyncio.Event()
    ownership_lost = asyncio.Event()

    async def _renew_loop() -> None:
        interval = max(1, lease_ttl // 3)
        while not renew_stop.is_set():
            try:
                renewed = await queue.renew_lease(job_id, fencing_token, extension_seconds=lease_ttl)
            except Exception:
                renewed = None  # transient Redis error — try again next tick
            if renewed is not False:
                try:
                    renewed = await renew_db_lease_open(
                        job_id=job_id,
                        fencing_token=fencing_token,
                        ttl_seconds=lease_ttl,
                    )
                except Exception:
                    renewed = None  # transient Postgres error — try again next tick
            if renewed is False:
                ownership_lost.set()
                return
            try:
                if await queue.update_progress(job_id, fencing_token=fencing_token) is False:
                    ownership_lost.set()
                    return
            except Exception as exc:
                logger.warning("Job {} progress heartbeat failed: {}", job_id, type(exc).__name__)
            try:
                await asyncio.wait_for(renew_stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    renew_task = asyncio.create_task(_renew_loop())
    exec_task = asyncio.create_task(
        execute_job(
            job_type,
            params,
            job_id=job_id,
            fencing_token=fencing_token,
            lease_prefix=queue.prefix,
        )
    )
    lost_wait = asyncio.create_task(ownership_lost.wait())
    try:
        done, _pending = await asyncio.wait(
            {exec_task, lost_wait}, return_when=asyncio.FIRST_COMPLETED
        )
        if lost_wait in done and not exec_task.done():
            exec_task.cancel()
            with suppress(asyncio.CancelledError):
                await exec_task
            raise StaleWorkerFencingError(
                f"Job {job_id} lease claimed by another worker; abandoned in-flight execution"
            )
        return exec_task.result()
    finally:
        renew_stop.set()
        for task in (renew_task, lost_wait, exec_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(renew_task, lost_wait, exec_task, return_exceptions=True)


async def process_one(queue: JobQueue) -> bool:
    lease_ttl = max(1, int(settings.WORKER_JOB_LEASE_SECONDS))
    try:
        res = await queue.dequeue(timeout=5, lease_ttl=lease_ttl)
        if isinstance(res, tuple):
            job_id, fencing_token = res
        else:
            job_id, fencing_token = res, None
    except Exception as exc:
        logger.warning(f"Redis dequeue failed (will retry): {exc}")
        await asyncio.sleep(1)
        return False

    if not job_id:
        return False

    job = await queue.get_job(job_id)
    if not job:
        logger.warning(f"Job {job_id} missing from store, skipping")
        await queue.ack(job_id, fencing_token=fencing_token)  # don't leave it stuck in the processing list
        return True

    if job.get("status") in {"completed", "degraded", "failed", "cancelled"}:
        await queue.ack(job_id, fencing_token=fencing_token)
        return True

    if fencing_token:
        # The Redis dequeue lease is only advisory — the Postgres lease row is
        # what actually fences domain commits. If another worker holds an
        # unexpired durable lease, it is executing this job; back off.
        try:
            claimed = await try_claim_db_lease(
                job_id=job_id,
                fencing_token=fencing_token,
                ttl_seconds=lease_ttl,
            )
        except Exception as exc:
            logger.warning(f"Job {job_id} durable lease claim errored ({exc}) — skipping this dequeue")
            # Keep the delivery recoverable after a transient database outage.
            return True
        if not claimed:
            logger.warning(f"Job {job_id} durable lease held by another worker — skipping this dequeue")
            # The Redis and durable leases can expire at different times.
            # Retain delivery until recovery can safely claim both.
            return True

    job_type = job["type"]
    params = job.get("params") or {}
    queue.available_capacity = 0
    attempt = await queue.start_attempt(job_id)
    max_attempts = max(int(job.get("max_attempts") or 1), 1)
    requested_timeout = int(job.get("timeout_seconds") or 0)
    job_timeout = (
        min(requested_timeout, settings.WORKER_MAX_JOB_TIMEOUT_SECONDS)
        if requested_timeout > 0
        else settings.WORKER_JOB_TIMEOUT_SECONDS
    )
    logger.info(f"Running job {job_id} ({job_type}) attempt={attempt}/{max_attempts}")
    async def check_lease():
        if fencing_token:
            await validate_task_fencing(queue.redis, queue.prefix, job_id, fencing_token)

    async def report(progress):
        await check_lease()
        if await queue.update_progress(job_id, progress, fencing_token=fencing_token) is False:
            raise StaleWorkerFencingError("Progress rejected after worker lease loss")

    async def enqueue_repair(params):
        await check_lease()
        repair_queue = queue_for_job(queue.redis, settings.WORKER_REDIS_PREFIX, "embedding_backfill", pools_enabled=True) \
            if settings.BRAIN_WORKER_POOLS_V2_ENABLED else queue
        return await repair_queue.enqueue("embedding_backfill", {**params, "causal_parent_job_id": job_id},
                                          idempotency_key=f"reindex-repair:{job_id}", max_attempts=3)

    runtime_token = current_job.set(JobRuntime(job_id, report, check_lease, enqueue_repair))
    acknowledge_delivery = False
    try:
        if attempt > max_attempts:
            await _finish_status(
                queue,
                job_id,
                fencing_token,
                JobStatus.FAILED,
                error=f"Exceeded {max_attempts} attempts after worker crash recovery",
            )
            acknowledge_delivery = True
            queue.available_capacity = 1
            return True

        try:
            if fencing_token:
                result = await asyncio.wait_for(
                    _execute_owned_job(queue, job_id, fencing_token, job_type, params, lease_ttl),
                    timeout=max(1, job_timeout),
                )
            else:
                result = await asyncio.wait_for(
                    execute_job(job_type, params),
                    timeout=max(1, job_timeout),
                )
            status = JobStatus.DEGRADED if result.get("status") == "degraded" else JobStatus.COMPLETED
            await _finish_status(queue, job_id, fencing_token, status, result=result)
            acknowledge_delivery = True
            mark_completed = getattr(queue, "mark_completed", None)
            if mark_completed is not None:
                completion_marker = mark_completed()
                if asyncio.iscoroutine(completion_marker):
                    await completion_marker
            logger.info(f"Job {job_id} completed")
        except StaleWorkerFencingError as exc:
            # Ownership moved to another worker: it owns retries/completion now.
            logger.warning(f"Job {job_id} abandoned — {exc}")
        except PermanentJobError as exc:
            logger.error(f"Job {job_id} failed permanently: {exc}")
            await _finish_status(
                queue,
                job_id,
                fencing_token,
                JobStatus.FAILED,
                error=str(exc),
                result=exc.result,
            )
            acknowledge_delivery = True
        except Exception as exc:
            if isinstance(exc, asyncio.TimeoutError):
                exc = RuntimeError(f"Job exceeded {job_timeout}s timeout")
            logger.exception(f"Job {job_id} failed: {exc}")
            if attempt < max_attempts:
                delay_seconds = min(5 * (2 ** (attempt - 1)), 300)
                retry_args: dict = {"fencing_token": fencing_token} if fencing_token else {}
                scheduled = await queue.schedule_retry(job_id, str(exc), delay_seconds, **retry_args)
                if scheduled is False:
                    logger.warning(f"Job {job_id} retry rejected — lease claimed by another worker")
                else:
                    acknowledge_delivery = True
                    logger.warning(
                        f"Job {job_id} scheduled for retry in {delay_seconds}s (attempt {attempt}/{max_attempts})"
                    )
            else:
                await _finish_status(queue, job_id, fencing_token, JobStatus.FAILED, error=str(exc))
                acknowledge_delivery = True
    except StaleWorkerFencingError as exc:
        logger.warning("Job {} transition stopped after lease loss: {}", job_id, exc)
    finally:
        current_job.reset(runtime_token)
        queue.available_capacity = 1
        if acknowledge_delivery:
            await queue.ack(job_id, fencing_token=fencing_token)
        if fencing_token and acknowledge_delivery:
            try:
                await release_db_lease_open(job_id=job_id, fencing_token=fencing_token)
            except Exception:
                pass  # lease row self-expires; release is best-effort
    return True


async def _heartbeat_loop(queue: JobQueue, stop: asyncio.Event) -> None:
    queue.heartbeat_ttl_seconds = settings.WORKER_HEARTBEAT_TTL_SECONDS
    interval = max(5, settings.WORKER_HEARTBEAT_TTL_SECONDS // 3)
    while not stop.is_set():
        payload = {
            "at": datetime.now(timezone.utc).isoformat(),
            "build": build_info(),
            "pool": getattr(queue, "pool_name", "maintenance"),
            "available_capacity": int(getattr(queue, "available_capacity", 1)),
            "last_completed_at": await queue.redis.get(queue.last_completed_key),
            "queue": await queue.get_capacity_stats(),
        }
        if isinstance(payload["last_completed_at"], bytes):
            payload["last_completed_at"] = payload["last_completed_at"].decode()
        try:
            await queue.heartbeat(json.dumps(payload))
        except Exception as exc:
            logger.warning(f"Worker heartbeat failed: {type(exc).__name__}")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue


async def run_worker() -> None:
    try:
        await init_db()
    except Exception as exc:
        # Redis is the queue dependency. Keep consuming self-diagnosis jobs when
        # PostgreSQL/Neo4j is down so the owner still receives the outage.
        logger.error(f"Worker schema initialization deferred: {type(exc).__name__}: {exc}")
    validated_index = None
    if settings.LATE_INTERACTION_REMOTE_ENABLED:
        try:
            validated_index = await validate_remote_late_interaction_release()
        except LateInteractionReleaseUnavailableError as exc:
            # Keep baseline indexing and jobs alive while the optional precision
            # sidecar is transiently unavailable. Identity mismatches remain fatal.
            logger.warning(
                "Worker late-interaction sidecar unavailable at startup; continuing fail-open: {}",
                str(exc),
            )
    if validated_index is not None:
        logger.info(
            "Worker late-interaction release validated: repo={} index={}",
            validated_index.repository_id,
            validated_index.index_revision,
        )
    pool = settings.WORKER_POOL_NAME
    queue = JobQueue(
        redis_client,
        prefix=worker_pool_prefix(
            settings.WORKER_REDIS_PREFIX,
            pool,
            enabled=settings.BRAIN_WORKER_POOLS_V2_ENABLED,
        ),
    )
    queue.pool_name = pool
    queue.available_capacity = 1
    recovered = await queue.recover_stale()
    if recovered:
        logger.warning(f"Requeued {recovered} stale job(s) from a previous crash")
    logger.info(f"Worker started (pool={pool}, prefix={queue.prefix})")
    stop = asyncio.Event()
    heartbeat_task = asyncio.create_task(_heartbeat_loop(queue, stop))

    # Graceful shutdown: handle SIGTERM/SIGINT so in-flight jobs finish cleanly.
    import signal

    def _request_shutdown(sig, frame):
        logger.info(f"Worker received {signal.Signals(sig).name}, finishing current job...")
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _request_shutdown)
        except (OSError, ValueError):
            pass  # not available on this platform/thread

    try:
        while not stop.is_set():
            await queue.recover_stale()
            await process_one(queue)
    finally:
        stop.set()
        heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task
        await queue.clear_heartbeat()
        try:
            await close_late_interaction_client()
        finally:
            await close_database_connections()
        logger.info("Worker shut down gracefully")


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
