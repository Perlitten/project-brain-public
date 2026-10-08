"""Redis list queue with per-job status hashes."""

from __future__ import annotations

import json
import hashlib
import time
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Tuple, cast

from redis.asyncio import Redis
from loguru import logger

from brain.config.settings import settings
from brain.version import build_info


WORKER_POOLS = ("fast", "maintenance", "deep")
_FAST_JOB_TYPES = {"health_check", "self_diagnosis", "proactive_insights"}

_RedisMapping = Mapping[
    bytes | bytearray | memoryview | str | int | float,
    bytes | bytearray | memoryview | str | int | float,
]
_DEEP_JOB_TYPES = {"deep_context", "nightly_maintenance", "diff_review"}


def worker_pool_for_job(job_type: str) -> str:
    if job_type in _FAST_JOB_TYPES:
        return "fast"
    if job_type in _DEEP_JOB_TYPES:
        return "deep"
    return "maintenance"


def worker_pool_prefix(base_prefix: str, pool: str, *, enabled: bool) -> str:
    if pool not in WORKER_POOLS:
        raise ValueError(f"Unknown worker pool: {pool}")
    return f"{base_prefix}:{pool}" if enabled else base_prefix


def queue_for_job(redis: Redis, base_prefix: str, job_type: str, *, pools_enabled: bool) -> "JobQueue":
    return JobQueue(
        redis,
        prefix=worker_pool_prefix(base_prefix, worker_pool_for_job(job_type), enabled=pools_enabled),
    )


async def worker_pool_status(
    redis: Redis,
    base_prefix: str,
    *,
    pools_enabled: bool,
) -> dict[str, dict[str, Any]]:
    """Report queue depth and *actual* serving capacity for readiness checks."""
    pools = WORKER_POOLS if pools_enabled else ("maintenance",)
    result: dict[str, dict[str, Any]] = {}
    for pool in pools:
        queue = JobQueue(redis, prefix=worker_pool_prefix(base_prefix, pool, enabled=pools_enabled))
        stats = await queue.get_capacity_stats()
        raw_heartbeat = await redis.get(queue.heartbeat_key)
        heartbeat: dict[str, Any] = {}
        if raw_heartbeat:
            try:
                heartbeat = json.loads(raw_heartbeat.decode() if isinstance(raw_heartbeat, bytes) else raw_heartbeat)
            except (TypeError, json.JSONDecodeError):
                heartbeat = {}
        capacity = int(heartbeat.get("available_capacity") or 0)
        result[pool] = {
            **stats,
            "heartbeat": bool(raw_heartbeat),
            "available_capacity": capacity,
            "last_completed_at": heartbeat.get("last_completed_at"),
            "build": heartbeat.get("build"),
            # A non-empty queue with no worker is degraded even though Redis is
            # healthy; it must never produce a false-ready response.
            "ready": bool(raw_heartbeat) and capacity > 0,
        }
    return result


class QueueDepthExceeded(RuntimeError):
    """Raised when enqueue would push the queue past WORKER_QUEUE_MAX_DEPTH."""

    def __init__(self, depth: int, limit: int) -> None:
        super().__init__(f"job queue depth budget exceeded: {depth} >= {limit}")
        self.depth = depth
        self.limit = limit


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRYING = "retrying"
    COMPLETED = "completed"
    DEGRADED = "degraded"
    FAILED = "failed"
    CANCELLED = "cancelled"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_serializer(obj: Any) -> str:
    """Fallback serializer for json.dumps — prevents TypeError on datetime etc."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, "__str__"):
        return str(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


class JobQueue:
    """Thin Redis queue over the worker prefix."""

    def __init__(self, redis: Redis, prefix: str = "brain:worker") -> None:
        self.redis = redis
        self.prefix = prefix
        self.pool_name = "maintenance"
        self.available_capacity = 1
        self.heartbeat_ttl_seconds = 60
        self.queue_key = f"{prefix}:queue"
        self.processing_key = f"{prefix}:processing"
        self.retry_key = f"{prefix}:retry"
        self.index_key = f"{prefix}:index"
        self.sequence_key = f"{prefix}:sequence"
        self.heartbeat_key = f"{prefix}:heartbeat"
        self.last_completed_key = f"{prefix}:last_completed"

    def _job_key(self, job_id: str) -> str:
        return f"{self.prefix}:job:{job_id}"

    def _lease_key(self, job_id: str) -> str:
        return f"{self.prefix}:job:{job_id}:lease"

    async def enqueue(
        self,
        job_type: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        idempotency_key: Optional[str] = None,
        max_attempts: int = 1,
        timeout_seconds: int | None = None,
    ) -> str:
        job_id = str(uuid.uuid4())
        idempotency_redis_key = None
        if idempotency_key:
            digest = hashlib.sha256(f"{job_type}:{idempotency_key}".encode("utf-8")).hexdigest()
            idempotency_redis_key = f"{self.prefix}:idempotency:{digest}"
            claimed = await self.redis.set(
                idempotency_redis_key,
                job_id,
                nx=True,
                ex=7 * 24 * 60 * 60,
            )
            if not claimed:
                existing = await self.redis.get(idempotency_redis_key)
                if existing:
                    existing_id = existing.decode() if isinstance(existing, bytes) else str(existing)
                    existing_job = await self.get_job(existing_id)
                    if existing_job and existing_job.get("status") != JobStatus.FAILED.value:
                        return existing_id
                    released = await self._release_idempotency_claim(
                        idempotency_redis_key,
                        existing_id,
                    )
                    if released:
                        claimed = await self.redis.set(
                            idempotency_redis_key,
                            job_id,
                            nx=True,
                            ex=7 * 24 * 60 * 60,
                        )
                    if not claimed:
                        winner = await self.redis.get(idempotency_redis_key)
                        if winner:
                            return winner.decode() if isinstance(winner, bytes) else str(winner)
                if not claimed:
                    claimed = await self.redis.set(
                        idempotency_redis_key,
                        job_id,
                        nx=True,
                        ex=7 * 24 * 60 * 60,
                    )
                    if not claimed:
                        winner = await self.redis.get(idempotency_redis_key)
                        if winner:
                            return winner.decode() if isinstance(winner, bytes) else str(winner)
                        raise RuntimeError("Could not establish an idempotency claim")
        now = _utc_now()
        max_attempts = max(1, min(int(max_attempts or 1), 10))
        timeout_seconds = max(0, int(timeout_seconds or 0))
        enqueued_build_sha = str(build_info().get("build_sha") or "unknown")
        job_data = {
            "id": job_id,
            "type": job_type,
            "status": JobStatus.QUEUED.value,
            "params": json.dumps(params or {}),
            "result": "",
            "error": "",
            "attempt": "0",
            "max_attempts": str(max_attempts),
            "timeout_seconds": str(timeout_seconds),
            "idempotency_key": idempotency_key or "",
            "enqueued_build_sha": enqueued_build_sha,
            "worker_build_sha": "",
            "created_at": now,
            "started_at": "",
            "finished_at": "",
            "next_attempt_at": "",
            "updated_at": now,
            "progress": "{}",
            "heartbeat_at": "",
        }
        try:
            max_depth = int(settings.WORKER_QUEUE_MAX_DEPTH)
            if max_depth > 0:
                # Check capacity and publish together: a read followed by a
                # pipeline allows concurrent producers to exceed the budget.
                depth = int(await self.redis.eval(
                    "local depth = redis.call('llen', KEYS[1]) + "
                    "redis.call('llen', KEYS[2]) + redis.call('zcard', KEYS[3]) "
                    "if depth >= tonumber(ARGV[1]) then return depth end "
                    "for i = 4, #ARGV, 2 do "
                    "redis.call('hset', KEYS[4], ARGV[i], ARGV[i + 1]) end "
                    "redis.call('expire', KEYS[4], ARGV[3]) "
                    "local score = redis.call('incr', KEYS[6]) "
                    "redis.call('zadd', KEYS[5], score, ARGV[2]) "
                    "redis.call('expire', KEYS[5], ARGV[3]) "
                    "redis.call('expire', KEYS[6], ARGV[3]) "
                    "redis.call('lpush', KEYS[1], ARGV[2]); return -1",
                    6, self.queue_key, self.processing_key, self.retry_key,
                    self._job_key(job_id), self.index_key, self.sequence_key,
                    str(max_depth), job_id, str(30 * 24 * 60 * 60),
                    *(value for pair in job_data.items() for value in pair),
                ))
                if depth >= 0:
                    raise QueueDepthExceeded(depth, max_depth)
            else:
                index_score = await self.redis.incr(self.sequence_key)
                pipe = self.redis.pipeline()
                pipe.hset(self._job_key(job_id), mapping=cast(Mapping[Any, str], job_data))
                pipe.expire(self._job_key(job_id), 30 * 24 * 60 * 60)
                pipe.zadd(self.index_key, {job_id: index_score})
                pipe.expire(self.index_key, 30 * 24 * 60 * 60)
                pipe.expire(self.sequence_key, 30 * 24 * 60 * 60)
                pipe.lpush(self.queue_key, job_id)
                await pipe.execute()
        except Exception:
            if idempotency_redis_key:
                await self._release_idempotency_claim(idempotency_redis_key, job_id)
            raise
        return job_id

    async def _release_idempotency_claim(self, key: str, expected_job_id: str) -> bool:
        """Compare-and-delete one claim without deleting a concurrent winner."""
        script = (
            "if redis.call('get', KEYS[1]) == ARGV[1] then "
            "return redis.call('del', KEYS[1]) else return 0 end"
        )
        return bool(await self.redis.eval(script, 1, key, expected_job_id))

    async def dequeue(
        self,
        timeout: int = 5,
        worker_id: Optional[str] = None,
        lease_ttl: int = 60,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Atomically dequeue a job and claim a worker lease with a fencing token.

        Returns (job_id, fencing_token) or (None, None) if queue is empty.
        """
        await self.promote_due_retries()
        job_id = cast(
            Optional[str],
            await self.redis.brpoplpush(self.queue_key, self.processing_key, timeout=timeout),
        )
        if not job_id:
            return None, None

        fencing_token = uuid.uuid4().hex
        w_id = worker_id or f"worker:{fencing_token[:8]}"
        lease_key = self._lease_key(job_id)
        # A duplicated queue delivery must not overwrite a live worker's lease.
        claimed = await self.redis.eval(
            "local expiry = tonumber(redis.call('hget', KEYS[1], 'lease_expiry') or '0') "
            "if expiry > tonumber(ARGV[1]) then "
            "redis.call('lrem', KEYS[2], 1, ARGV[6]); return 0 end "
            "redis.call('hset', KEYS[1], 'worker_id', ARGV[2], 'fencing_token', ARGV[3], "
            "'lease_expiry', ARGV[4], 'heartbeat_at', ARGV[1]) "
            "redis.call('expire', KEYS[1], ARGV[5]); return 1",
            2, lease_key, self.processing_key, str(time.time()), w_id, fencing_token,
            str(time.time() + lease_ttl), str(lease_ttl * 2), job_id,
        )
        if not claimed:
            return None, None
        return job_id, fencing_token

    async def renew_lease(
        self,
        job_id: str,
        fencing_token: str,
        extension_seconds: int = 60,
    ) -> bool:
        """Renew worker lease heartbeat. Returns True if lease was extended."""
        script = (
            "if redis.call('hget', KEYS[1], 'fencing_token') == ARGV[1] then "
            "redis.call('hset', KEYS[1], 'lease_expiry', ARGV[2]) "
            "redis.call('hset', KEYS[1], 'heartbeat_at', ARGV[3]) "
            "redis.call('expire', KEYS[1], ARGV[4]) "
            "return 1 else return 0 end"
        )
        new_expiry = str(time.time() + extension_seconds)
        now_str = str(time.time())
        ttl_str = str(extension_seconds * 2)
        res = await self.redis.eval(script, 1, self._lease_key(job_id), fencing_token, new_expiry, now_str, ttl_str)
        return bool(res)

    async def promote_due_retries(self, limit: int = 100) -> int:
        due = await self.redis.zrangebyscore(
            self.retry_key,
            min="-inf",
            max=time.time(),
            start=0,
            num=max(1, min(limit, 1000)),
        )
        promoted = 0
        for job_id in due:
            if not isinstance(job_id, (str, bytes)):
                continue
            if await self.redis.zrem(self.retry_key, job_id):
                await self.redis.lpush(self.queue_key, job_id)
                promoted += 1
        return promoted

    async def ack(self, job_id: str, fencing_token: Optional[str] = None) -> bool:
        """Remove a finished job from processing list. If fencing_token is given, verify lease ownership."""
        if fencing_token:
            current_token = await self.redis.hget(self._lease_key(job_id), "fencing_token")
            if isinstance(current_token, bytes):
                current_token = current_token.decode()
            if current_token != fencing_token:
                logger.warning(f"Fencing token mismatch on ack for job {job_id}: active={current_token}, given={fencing_token}")
                return False
            return bool(await self.redis.eval(
                "if redis.call('hget', KEYS[1], 'fencing_token') ~= ARGV[2] then return 0 end "
                "redis.call('lrem', KEYS[2], 0, ARGV[1]); redis.call('del', KEYS[1]); return 1",
                2, self._lease_key(job_id), self.processing_key, job_id, fencing_token,
            ))
        await self.redis.lrem(self.processing_key, 0, job_id)
        await self.redis.delete(self._lease_key(job_id))
        return True

    async def recover_stale(self, lease_buffer: float = 5.0) -> int:
        """Requeue only jobs whose worker lease has actually expired.

        Prevents active concurrent workers from having live jobs stolen.
        """
        processing_jobs = await self.redis.lrange(self.processing_key, 0, -1)
        moved = 0
        now = time.time()
        for raw_job_id in processing_jobs:
            job_id = raw_job_id.decode() if isinstance(raw_job_id, bytes) else raw_job_id
            lease_key = self._lease_key(job_id)
            expiry_raw = await self.redis.hget(lease_key, "lease_expiry")
            lease_expired = True
            if expiry_raw:
                try:
                    exp_val = float(expiry_raw.decode() if isinstance(expiry_raw, bytes) else expiry_raw)
                    if exp_val > (now - lease_buffer):
                        lease_expired = False
                except ValueError:
                    lease_expired = True

            if lease_expired:
                # Recheck atomically: renewal may have raced the advisory read.
                recovered = await self.redis.eval(
                    "local expiry = tonumber(redis.call('hget', KEYS[1], 'lease_expiry') or '0') "
                    "if expiry > tonumber(ARGV[1]) then return 0 end "
                    "if redis.call('lrem', KEYS[2], 1, ARGV[2]) == 0 then return 0 end "
                    "redis.call('del', KEYS[1]); redis.call('lpush', KEYS[3], ARGV[2]); return 1",
                    3, lease_key, self.processing_key, self.queue_key, str(now - lease_buffer), job_id,
                )
                if recovered:
                    moved += 1
        return moved

    async def start_attempt(self, job_id: str) -> int:
        attempt = await self.redis.hincrby(self._job_key(job_id), "attempt", 1)
        await self.update_status(
            job_id,
            JobStatus.RUNNING,
            extra={
                "started_at": _utc_now(),
                "next_attempt_at": "",
                "worker_build_sha": str(build_info().get("build_sha") or "unknown"),
            },
        )
        return int(attempt)

    async def schedule_retry(
        self,
        job_id: str,
        error: str,
        delay_seconds: int,
        *,
        fencing_token: Optional[str] = None,
    ) -> bool:
        """Schedule a retry. With ``fencing_token``, returns False when the
        fenced status write was rejected — no retry entry is queued then."""
        due_at = time.time() + max(1, int(delay_seconds))
        written = await self.update_status(
            job_id,
            JobStatus.RETRYING,
            error=error,
            extra={"next_attempt_at": datetime.fromtimestamp(due_at, tz=timezone.utc).isoformat()},
            fencing_token=fencing_token,
        )
        if not written:
            return False
        await self.redis.zadd(
            self.retry_key,
            {job_id: due_at},
        )
        return True

    async def cancel(self, job_id: str) -> bool:
        """Cancel a job that has not started. Atomic: the write only lands
        while the stored status is still ``queued`` — a job a worker has
        already dequeued is left for the worker's own fenced terminal write,
        and the caller gets ``False``. The pending queue entry and any
        idempotency claim are released so the same work can be submitted
        again right away."""
        job = await self.get_job(job_id)
        if job is None:
            return False
        written = await self.redis.eval(
            "if redis.call('hget', KEYS[1], 'status') == ARGV[1] then "
            "if redis.call('lpos', KEYS[3], ARGV[4]) then return 0 end "
            "if redis.call('lrem', KEYS[2], 1, ARGV[4]) ~= 1 then return 0 end "
            "redis.call('hset', KEYS[1], 'status', ARGV[2], 'finished_at', ARGV[3], 'updated_at', ARGV[3]) "
            "return 1 end "
            "return 0",
            3,
            self._job_key(job_id),
            self.queue_key,
            self.processing_key,
            JobStatus.QUEUED.value,
            JobStatus.CANCELLED.value,
            _utc_now(),
            job_id,
        )
        if not written:
            return False
        if job.get("idempotency_key"):
            digest = hashlib.sha256(
                f"{job.get('type')}:{job['idempotency_key']}".encode("utf-8")
            ).hexdigest()
            await self._release_idempotency_claim(
                f"{self.prefix}:idempotency:{digest}", job_id
            )
        return True

    async def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        raw = cast(Dict[str, str], await self.redis.hgetall(self._job_key(job_id)))
        if not raw:
            return None
        return self._deserialize_job(raw)

    # Atomically verify the caller's fencing token and apply the status write.
    # KEYS[1] = job hash, KEYS[2] = lease hash; ARGV[1] = token,
    # ARGV[2..#ARGV-1] = field/value pairs, ARGV[#ARGV] = job-key expire seconds.
    _FENCED_UPDATE_SCRIPT = (
        "if redis.call('hget', KEYS[2], 'fencing_token') == ARGV[1] then "
        "for i = 2, #ARGV - 1, 2 do "
        "redis.call('hset', KEYS[1], ARGV[i], ARGV[i + 1]) "
        "end "
        "redis.call('expire', KEYS[1], ARGV[#ARGV]) "
        "return 1 else return 0 end"
    )

    async def update_status(
        self,
        job_id: str,
        status: JobStatus,
        *,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
        extra: Optional[Dict[str, str]] = None,
        fencing_token: Optional[str] = None,
    ) -> bool:
        """Write job status. With ``fencing_token``, the write only lands when the
        lease still belongs to that token — a stale worker cannot overwrite a
        job another worker has claimed. Returns False when the fenced write was
        rejected; always True without a token."""
        mapping: Dict[str, str] = {
            "status": status.value,
            "updated_at": _utc_now(),
        }
        if result is not None:
            mapping["result"] = json.dumps(result, default=_json_serializer)
        if status in {JobStatus.COMPLETED, JobStatus.DEGRADED}:
            mapping["error"] = ""
            mapping["finished_at"] = _utc_now()
        elif status == JobStatus.FAILED:
            mapping["finished_at"] = _utc_now()
        if error is not None:
            mapping["error"] = error
        if extra:
            mapping.update(extra)
        if fencing_token:
            argv: list[str] = [fencing_token]
            for field, value in mapping.items():
                argv.extend([field, str(value)])
            argv.append(str(30 * 24 * 60 * 60))
            applied = await self.redis.eval(
                self._FENCED_UPDATE_SCRIPT,
                2,
                self._job_key(job_id),
                self._lease_key(job_id),
                *argv,
            )
            if not applied:
                logger.warning(
                    f"Fenced status write rejected for job {job_id}: lease owned by another worker"
                )
                return False
            return True
        pipe = self.redis.pipeline()
        pipe.hset(self._job_key(job_id), mapping=cast(Mapping[Any, str], mapping))
        pipe.expire(self._job_key(job_id), 30 * 24 * 60 * 60)
        await pipe.execute()
        return True

    async def update_progress(self, job_id: str, progress: dict | None = None, *,
                              fencing_token: str | None = None) -> bool:
        mapping = {"updated_at": _utc_now(), "heartbeat_at": _utc_now()}
        if progress is not None:
            mapping["progress"] = json.dumps(progress, default=_json_serializer)
        if fencing_token:
            argv = [fencing_token]
            for field, value in mapping.items():
                argv.extend([field, value])
            argv.append(str(30 * 24 * 60 * 60))
            return bool(await self.redis.eval(self._FENCED_UPDATE_SCRIPT, 2, self._job_key(job_id),
                                             self._lease_key(job_id), *argv))
        await self.redis.hset(self._job_key(job_id), mapping=cast(_RedisMapping, mapping))
        return True

    async def get_stats(self) -> Dict[str, int]:
        """Legacy compact queue state retained for dashboard/API compatibility."""
        pipe = self.redis.pipeline()
        pipe.llen(self.queue_key)
        pipe.llen(self.processing_key)
        pipe.zcard(self.retry_key)
        queued, processing, retrying = await pipe.execute()
        return {
            "queued": int(queued),
            "processing": int(processing),
            "retrying": int(retrying),
        }

    async def get_capacity_stats(self) -> Dict[str, int]:
        """Extended readiness data without changing the legacy ``get_stats`` contract."""
        stats = await self.get_stats()
        async def oldest_age(key: str, timestamp_field: str) -> int:
            lindex = getattr(self.redis, "lindex", None)
            job_id_raw = await lindex(key, -1) if lindex is not None else None
            if not job_id_raw:
                return 0
            job_id = job_id_raw.decode() if isinstance(job_id_raw, bytes) else str(job_id_raw)
            job = await self.get_job(job_id)
            try:
                timestamp = datetime.fromisoformat(str((job or {}).get(timestamp_field)))
                return max(0, int((datetime.now(timezone.utc) - timestamp).total_seconds()))
            except (TypeError, ValueError):
                return -1

        oldest_queued_age_seconds = await oldest_age(self.queue_key, "created_at")
        oldest_processing_age_seconds = await oldest_age(self.processing_key, "started_at")
        return {
            **stats,
            "oldest_queued_age_seconds": oldest_queued_age_seconds,
            "oldest_processing_age_seconds": oldest_processing_age_seconds,
        }

    async def recent_job_ids(self, limit: int = 100, *, offset: int = 0) -> list[str]:
        batch_size = max(1, min(int(limit), 1000))
        start = max(0, int(offset))
        values = await self.redis.zrevrange(
            self.index_key,
            start,
            start + batch_size - 1,
        )
        return [value.decode() if isinstance(value, bytes) else str(value) for value in values]

    async def heartbeat(self, value: str) -> None:
        await self.redis.set(
            self.heartbeat_key,
            value,
            ex=max(10, int(getattr(self, "heartbeat_ttl_seconds", 45))),
        )

    async def clear_heartbeat(self) -> None:
        await self.redis.delete(self.heartbeat_key)

    async def mark_completed(self) -> None:
        await self.redis.set(self.last_completed_key, _utc_now(), ex=30 * 24 * 60 * 60)

    @staticmethod
    def _deserialize_job(raw: Dict[str, str]) -> Dict[str, Any]:
        job: Dict[str, Any] = dict(raw)
        params_raw = job.pop("params", "") or "{}"
        result_raw = job.pop("result", "") or ""
        progress_raw = job.pop("progress", "") or "{}"
        try:
            job["progress"] = json.loads(progress_raw)
        except (TypeError, json.JSONDecodeError):
            job["progress"] = {}
        try:
            job["params"] = json.loads(params_raw)
        except json.JSONDecodeError:
            job["params"] = {}
        if result_raw:
            try:
                job["result"] = json.loads(result_raw)
            except json.JSONDecodeError:
                job["result"] = result_raw
        else:
            job["result"] = None
        for field in ("attempt", "max_attempts", "timeout_seconds"):
            try:
                job[field] = int(job.get(field) or 0)
            except (TypeError, ValueError):
                job[field] = 0
        return job
