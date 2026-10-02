from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import brain.workers.queue as queue_module
from brain.workers.queue import JobQueue, JobStatus
from brain.workers.errors import PermanentJobError
from brain.workers.worker import process_one


@pytest.mark.asyncio
async def test_heartbeat_transport_failure_does_not_cancel_execution(monkeypatch):
    redis = _FakeRedis()
    queue = JobQueue(redis)
    job_id = await queue.enqueue("reindex")
    monkeypatch.setattr(queue, "renew_lease", AsyncMock(side_effect=ConnectionError("Redis unavailable")))
    async def execute(*args, **kwargs):
        await asyncio.sleep(0)
        return {"status": "completed"}
    monkeypatch.setattr("brain.workers.worker.execute_job", execute)
    assert await process_one(queue)
    assert (await queue.get_job(job_id))["status"] == "completed"
    assert queue.renew_lease.await_count > 0


@pytest.mark.asyncio
async def test_durable_claim_transport_failure_keeps_delivery_recoverable(monkeypatch):
    redis = _FakeRedis()
    queue = JobQueue(redis)
    job_id = await queue.enqueue("reindex")
    monkeypatch.setattr("brain.workers.worker.try_claim_db_lease", AsyncMock(side_effect=ConnectionError("database down")))
    assert await process_one(queue)
    assert (await queue.get_job(job_id))["status"] == "queued"
    assert job_id in await redis.lrange(queue.processing_key, 0, -1)
    redis.hashes[queue._lease_key(job_id)]["lease_expiry"] = "0"
    assert await queue.recover_stale() == 1


@pytest.mark.asyncio
async def test_stale_delivery_cannot_replace_current_progress():
    redis = _FakeRedis()
    queue = JobQueue(redis)
    job_id = await queue.enqueue("reindex")
    _, token = await queue.dequeue(timeout=0)
    assert await queue.update_progress(job_id, {"phase": "files"}, fencing_token=token)
    redis.hashes[queue._lease_key(job_id)]["fencing_token"] = "new-owner"
    assert not await queue.update_progress(job_id, {"phase": "completed"}, fencing_token=token)
    assert (await queue.get_job(job_id))["progress"] == {"phase": "files"}


class _Pipeline:
    def __init__(self, redis):
        self.redis = redis
        self.operations = []

    def hset(self, key, *, mapping):
        self.operations.append(("hset", key, mapping))
        return self

    def expire(self, key, seconds):
        self.operations.append(("expire", key, seconds))
        return self

    def lpush(self, key, value):
        self.operations.append(("lpush", key, value))
        return self

    def set(self, key, value, **kwargs):
        self.operations.append(("set", key, value, kwargs))
        return self

    def llen(self, key):
        self.operations.append(("llen", key))
        return self

    def zcard(self, key):
        self.operations.append(("zcard", key))
        return self

    def zadd(self, key, mapping):
        self.operations.append(("zadd", key, mapping))
        return self

    async def execute(self):
        results = []
        for operation in self.operations:
            name, *args = operation
            if name == "expire":
                results.append(True)
                continue
            method = getattr(self.redis, name)
            results.append(await method(*args))
        return results


class _FakeRedis:
    def __init__(self):
        self.hashes = defaultdict(dict)
        self.values = {}
        self.lists = defaultdict(deque)
        self.sorted_sets = defaultdict(dict)

    def pipeline(self):
        return _Pipeline(self)

    async def expire(self, key, seconds):
        return True

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def get(self, key):
        return self.values.get(key)

    async def delete(self, key):
        value = self.values.pop(key, None)
        hashed = self.hashes.pop(key, None)
        return int(value is not None or hashed is not None)

    async def incr(self, key):
        value = int(self.values.get(key, 0)) + 1
        self.values[key] = value
        return value

    async def eval(self, _script, _num_keys, key, *args):
        if "redis.call('hget', KEYS[1], 'status') == ARGV[1]" in _script:
            # JobQueue.cancel: hset status iff still queued + lrem the pending entry
            queue, processing, expected, cancelled, now, job_id = args
            if self.hashes[key].get("status") != expected:
                return 0
            if job_id in self.lists[processing]:
                return 0
            if await self.lrem(queue, 1, job_id) != 1:
                return 0
            self.hashes[key].update(status=cancelled, finished_at=now, updated_at=now)
            return 1
        if _num_keys == 6:
            processing, retry, job, index, sequence, limit, job_id, _ttl, *pairs = args
            depth = len(self.lists[key]) + len(self.lists[processing]) + len(self.sorted_sets[retry])
            if depth >= int(limit):
                return depth
            self.hashes[job].update(zip(pairs[::2], pairs[1::2]))
            score = int(self.values.get(sequence, 0)) + 1
            self.values[sequence] = score
            self.sorted_sets[index][job_id] = score
            self.lists[key].appendleft(job_id)
            return -1
        if "for i = 2, #ARGV" in _script:
            lease_key, token, *values = args
            if self.hashes.get(lease_key, {}).get("fencing_token") != token:
                return 0
            pairs = values[:-1]
            for i in range(0, len(pairs), 2):
                self.hashes[key][pairs[i]] = pairs[i + 1]
            return 1
        if _num_keys == 2 and len(args) == 3:
            processing, job_id, token = args
            if self.hashes[key].get("fencing_token") != token:
                return 0
            await self.lrem(processing, 0, job_id)
            await self.delete(key)
            return 1
        if _num_keys == 3:
            processing, queue, cutoff, job_id = args
            if float(self.hashes[key].get("lease_expiry", 0)) > float(cutoff):
                return 0
            if job_id not in self.lists[processing]:
                return 0
            self.lists[processing].remove(job_id)
            await self.delete(key)
            await self.lpush(queue, job_id)
            return 1
        if _num_keys == 2:
            processing, now, worker, token, expiry, _ttl, job_id = args
            if float(self.hashes[key].get("lease_expiry", 0)) > float(now):
                items = self.lists[processing]
                items.remove(job_id)
                return 0
            self.hashes[key].update(worker_id=worker, fencing_token=token, lease_expiry=expiry, heartbeat_at=now)
            return 1
        if "lease_expiry" in _script:
            expected, expiry, now, _ttl = args
            if self.hashes[key].get("fencing_token") != expected:
                return 0
            self.hashes[key].update(lease_expiry=expiry, heartbeat_at=now)
            return 1
        expected, = args
        if self.values.get(key) != expected:
            return 0
        self.values.pop(key, None)
        return 1

    async def hset(self, key, mapping=None, **kwargs):
        self.hashes[key].update(dict(mapping))
        return len(mapping)

    async def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    async def hget(self, key, field):
        return self.hashes[key].get(field)

    async def lrange(self, key, start, end):
        return list(self.lists[key])[start:None if end == -1 else end + 1]

    async def hincrby(self, key, field, amount):
        value = int(self.hashes[key].get(field, 0)) + amount
        self.hashes[key][field] = str(value)
        return value

    async def lpush(self, key, value):
        self.lists[key].appendleft(value)
        return len(self.lists[key])

    async def llen(self, key):
        return len(self.lists[key])

    async def lrem(self, key, count, value):
        before = len(self.lists[key])
        self.lists[key] = deque(item for item in self.lists[key] if item != value)
        return before - len(self.lists[key])

    async def brpoplpush(self, source, destination, timeout=0):
        if not self.lists[source]:
            return None
        value = self.lists[source].pop()
        self.lists[destination].appendleft(value)
        return value

    async def rpoplpush(self, source, destination):
        return await self.brpoplpush(source, destination)

    async def zadd(self, key, mapping):
        self.sorted_sets[key].update(mapping)
        return len(mapping)

    async def zcard(self, key):
        return len(self.sorted_sets[key])

    async def zrangebyscore(self, key, min, max, start=0, num=100):
        ceiling = float(max)
        due = [
            item for item, score in sorted(self.sorted_sets[key].items(), key=lambda pair: pair[1]) if score <= ceiling
        ]
        return due[start : start + num]

    async def zrevrange(self, key, start, end):
        ordered = [
            item
            for item, _score in sorted(
                self.sorted_sets[key].items(),
                key=lambda pair: pair[1],
                reverse=True,
            )
        ]
        return ordered[start : end + 1]

    async def zrem(self, key, value):
        return int(self.sorted_sets[key].pop(value, None) is not None)


@pytest.mark.asyncio
async def test_enqueue_idempotency_returns_the_existing_job():
    redis = _FakeRedis()
    queue = JobQueue(redis)

    first = await queue.enqueue(
        "self_diagnosis",
        {"scheduled": True},
        idempotency_key="nightly:2026-07-28",
        max_attempts=3,
    )
    second = await queue.enqueue(
        "self_diagnosis",
        {"scheduled": True},
        idempotency_key="nightly:2026-07-28",
        max_attempts=3,
    )

    assert second == first
    assert await redis.llen(queue.queue_key) == 1
    job = await queue.get_job(first)
    assert job["max_attempts"] == 3
    assert job["attempt"] == 0


@pytest.mark.asyncio
async def test_queue_persists_per_job_timeout():
    redis = _FakeRedis()
    queue = JobQueue(redis)

    job_id = await queue.enqueue(
        "nightly_maintenance",
        timeout_seconds=21_600,
    )

    job = await queue.get_job(job_id)
    assert job["timeout_seconds"] == 21_600


@pytest.mark.asyncio
async def test_failed_idempotent_job_can_be_reenqueued():
    redis = _FakeRedis()
    queue = JobQueue(redis)

    failed_id = await queue.enqueue(
        "reindex",
        {"source_revision": "abc"},
        idempotency_key="git:abc",
        max_attempts=3,
    )
    await queue.update_status(failed_id, JobStatus.FAILED, error="permanent")
    retried_id = await queue.enqueue(
        "reindex",
        {"source_revision": "abc"},
        idempotency_key="git:abc",
        max_attempts=3,
    )

    assert retried_id != failed_id
    assert (await queue.get_job(retried_id))["status"] == "queued"


@pytest.mark.asyncio
async def test_recent_job_index_is_newest_first():
    redis = _FakeRedis()
    queue = JobQueue(redis)
    first = await queue.enqueue("health_check")
    second = await queue.enqueue("self_diagnosis")

    assert await queue.recent_job_ids() == [second, first]


@pytest.mark.asyncio
async def test_failed_attempt_is_delayed_then_promoted(monkeypatch):
    redis = _FakeRedis()
    queue = JobQueue(redis)
    clock = {"value": 100.0}
    monkeypatch.setattr(queue_module.time, "time", lambda: clock["value"])

    job_id = await queue.enqueue("health_check", max_attempts=3)
    dequeued_id, _ = await queue.dequeue(timeout=0)
    assert dequeued_id == job_id
    assert await queue.start_attempt(job_id) == 1
    await queue.schedule_retry(job_id, "temporary", 5)
    await queue.ack(job_id)

    assert (await queue.get_job(job_id))["status"] == "retrying"
    assert await queue.promote_due_retries() == 0
    clock["value"] = 106.0
    assert await queue.promote_due_retries() == 1
    assert (await queue.get_stats()) == {
        "queued": 1,
        "processing": 0,
        "retrying": 0,
    }


@pytest.mark.asyncio
async def test_completion_clears_previous_error_and_records_finish():
    redis = _FakeRedis()
    queue = JobQueue(redis)
    job_id = await queue.enqueue("health_check")
    await queue.update_status(job_id, JobStatus.RETRYING, error="old")
    await queue.update_status(job_id, JobStatus.COMPLETED, result={"ok": True})

    job = await queue.get_job(job_id)
    assert job["status"] == "completed"
    assert job["error"] == ""
    assert job["finished_at"]
    assert job["result"] == {"ok": True}


@pytest.mark.asyncio
async def test_duplicate_delivery_keeps_the_active_lease():
    redis = _FakeRedis()
    queue = JobQueue(redis)
    job_id = await queue.enqueue("reindex")
    _, token = await queue.dequeue(timeout=0)
    await redis.lpush(queue.queue_key, job_id)
    assert await queue.dequeue(timeout=0) == (None, None)
    assert await redis.hget(queue._lease_key(job_id), "fencing_token") == token
    assert await redis.llen(queue.processing_key) == 1
    assert await queue.ack(job_id, fencing_token="stale") is False
    assert await queue.ack(job_id, fencing_token=token) is True


@pytest.mark.asyncio
async def test_progress_replays_after_recovery_and_degraded_is_terminal(monkeypatch):
    redis = _FakeRedis()
    queue = JobQueue(redis)
    clock = {"value": 100.0}
    monkeypatch.setattr(queue_module.time, "time", lambda: clock["value"])
    job_id = await queue.enqueue("reindex")
    await queue.dequeue(timeout=0)
    progress = {"files": {"processed": 4, "discovered": 10}, "phase": "files"}
    await queue.update_progress(job_id, progress)
    assert await queue.recover_stale() == 0
    clock["value"] = 170.0
    assert await queue.recover_stale() == 1
    assert (await JobQueue(redis).get_job(job_id))["progress"] == progress
    await queue.update_status(job_id, JobStatus.DEGRADED, result={"repair_job_id": "child"})
    job = await queue.get_job(job_id)
    assert job["status"] == "degraded"
    assert job["finished_at"]
    with patch("brain.workers.worker.execute_job", new=AsyncMock()) as execute:
        await process_one(queue)
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_worker_schedules_retry_before_terminal_failure():
    queue = MagicMock()
    queue.dequeue = AsyncMock(return_value="job-1")
    queue.get_job = AsyncMock(
        return_value={
            "id": "job-1",
            "type": "health_check",
            "params": {},
            "max_attempts": 2,
        }
    )
    queue.start_attempt = AsyncMock(return_value=1)
    queue.schedule_retry = AsyncMock()
    queue.update_status = AsyncMock()
    queue.ack = AsyncMock()

    with patch(
        "brain.workers.worker.execute_job",
        new=AsyncMock(side_effect=RuntimeError("temporary")),
    ):
        assert await process_one(queue) is True

    queue.schedule_retry.assert_awaited_once()
    queue.update_status.assert_not_awaited()
    assert queue.ack.await_args[0][0] == "job-1"


@pytest.mark.asyncio
async def test_worker_marks_last_attempt_failed():
    queue = MagicMock()
    queue.dequeue = AsyncMock(return_value="job-1")
    queue.get_job = AsyncMock(
        return_value={
            "id": "job-1",
            "type": "health_check",
            "params": {},
            "max_attempts": 2,
        }
    )
    queue.start_attempt = AsyncMock(return_value=2)
    queue.schedule_retry = AsyncMock()
    queue.update_status = AsyncMock()
    queue.ack = AsyncMock()

    with patch(
        "brain.workers.worker.execute_job",
        new=AsyncMock(side_effect=RuntimeError("terminal")),
    ):
        assert await process_one(queue) is True

    queue.schedule_retry.assert_not_awaited()
    queue.update_status.assert_awaited_once_with("job-1", JobStatus.FAILED, error="terminal")
    assert queue.ack.await_args[0][0] == "job-1"


@pytest.mark.asyncio
async def test_worker_caps_requested_timeout_at_configured_maximum():
    queue = MagicMock()
    queue.dequeue = AsyncMock(return_value="job-1")
    queue.get_job = AsyncMock(
        return_value={
            "id": "job-1",
            "type": "nightly_maintenance",
            "params": {},
            "max_attempts": 1,
            "timeout_seconds": 99_999,
        }
    )
    queue.start_attempt = AsyncMock(return_value=1)
    queue.update_status = AsyncMock()
    queue.ack = AsyncMock()
    observed = {}

    async def capture_wait_for(awaitable, timeout):
        observed["timeout"] = timeout
        return await awaitable

    with (
        patch(
            "brain.workers.worker.execute_job",
            new=AsyncMock(return_value={"status": "completed"}),
        ),
        patch(
            "brain.workers.worker.asyncio.wait_for",
            new=capture_wait_for,
        ),
        patch(
            "brain.workers.worker.settings.WORKER_MAX_JOB_TIMEOUT_SECONDS",
            21_600,
        ),
    ):
        assert await process_one(queue) is True

    assert observed["timeout"] == 21_600
    queue.update_status.assert_awaited_once_with(
        "job-1",
        JobStatus.COMPLETED,
        result={"status": "completed"},
    )


@pytest.mark.asyncio
async def test_worker_does_not_retry_permanent_failure_and_keeps_partial_result():
    queue = MagicMock()
    queue.dequeue = AsyncMock(return_value="job-1")
    queue.get_job = AsyncMock(
        return_value={
            "id": "job-1",
            "type": "reindex",
            "params": {},
            "max_attempts": 3,
        }
    )
    queue.start_attempt = AsyncMock(return_value=1)
    queue.schedule_retry = AsyncMock()
    queue.update_status = AsyncMock()
    queue.ack = AsyncMock()

    with patch(
        "brain.workers.worker.execute_job",
        new=AsyncMock(
            side_effect=PermanentJobError(
                "revision mismatch",
                result={"commit_hash": "snapshot:abc:" + "d" * 64},
            )
        ),
    ):
        assert await process_one(queue) is True

    queue.schedule_retry.assert_not_awaited()
    queue.update_status.assert_awaited_once_with(
        "job-1",
        JobStatus.FAILED,
        error="revision mismatch",
        result={"commit_hash": "snapshot:abc:" + "d" * 64},
    )


@pytest.mark.asyncio
async def test_worker_fails_poison_job_after_crash_recovery_attempt_budget():
    queue = MagicMock()
    queue.dequeue = AsyncMock(return_value="job-1")
    queue.get_job = AsyncMock(
        return_value={
            "id": "job-1",
            "type": "health_check",
            "params": {},
            "max_attempts": 2,
        }
    )
    queue.start_attempt = AsyncMock(return_value=3)
    queue.update_status = AsyncMock()
    queue.ack = AsyncMock()

    execute = AsyncMock()
    with patch("brain.workers.worker.execute_job", new=execute):
        assert await process_one(queue) is True

    execute.assert_not_awaited()
    queue.update_status.assert_awaited_once_with(
        "job-1",
        JobStatus.FAILED,
        error="Exceeded 2 attempts after worker crash recovery",
    )
    assert queue.ack.await_args[0][0] == "job-1"


@pytest.fixture(autouse=True)
def _db_lease_granted(monkeypatch):
    """The durable Postgres lease is exercised in test_db_fencing.py — these

    tests cover the Redis queue semantics, so the DB calls are stubbed to a
    successful-ownership outcome."""
    monkeypatch.setattr(
        "brain.workers.worker.try_claim_db_lease", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        "brain.workers.worker.renew_db_lease_open", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        "brain.workers.worker.release_db_lease_open", AsyncMock(return_value=True)
    )


@pytest.mark.asyncio
async def test_renew_lease_extends_expiry_for_owner_only():
    redis = _FakeRedis()
    queue = JobQueue(redis)
    job_id = await queue.enqueue("health_check")
    dequeued_id, token = await queue.dequeue(timeout=0, lease_ttl=60)
    assert dequeued_id == job_id

    assert await queue.renew_lease(job_id, token, extension_seconds=60) is True
    assert await queue.renew_lease(job_id, "other-worker-token", extension_seconds=60) is False


@pytest.mark.asyncio
async def test_fenced_status_write_rejected_for_stale_worker():
    redis = _FakeRedis()
    queue = JobQueue(redis)
    job_id = await queue.enqueue("health_check")
    _, token_a = await queue.dequeue(timeout=0, lease_ttl=60)

    # Another worker takes the lease (e.g. after lease expiry + recover_stale).
    lease_key = queue._lease_key(job_id)
    redis.hashes[lease_key]["fencing_token"] = "token-B"

    written = await queue.update_status(job_id, JobStatus.COMPLETED, fencing_token=token_a)
    assert written is False
    job = await queue.get_job(job_id)
    assert job["status"] == "queued"  # stale worker could not commit terminal state


@pytest.mark.asyncio
async def test_fenced_status_write_applies_for_owner():
    redis = _FakeRedis()
    queue = JobQueue(redis)
    job_id = await queue.enqueue("health_check")
    _, token = await queue.dequeue(timeout=0, lease_ttl=60)

    written = await queue.update_status(job_id, JobStatus.COMPLETED, result={"ok": True}, fencing_token=token)
    assert written is True
    job = await queue.get_job(job_id)
    assert job["status"] == "completed"


@pytest.mark.asyncio
async def test_worker_abandons_job_when_lease_is_stolen(monkeypatch):
    redis = _FakeRedis()
    queue = JobQueue(redis)
    job_id = await queue.enqueue("health_check", max_attempts=3)
    monkeypatch.setattr("brain.workers.worker.settings.WORKER_JOB_LEASE_SECONDS", 3)

    started = asyncio.Event()

    async def steal_lease_mid_run(*args, **kwargs):
        started.set()
        # Simulate another worker claiming the job after lease expiry.
        redis.hashes[queue._lease_key(job_id)]["fencing_token"] = "token-B"
        await asyncio.sleep(60)
        return {"status": "completed"}

    with patch("brain.workers.worker.execute_job", new=AsyncMock(side_effect=steal_lease_mid_run)):
        assert await process_one(queue) is True

    assert started.is_set()
    job = await queue.get_job(job_id)
    assert job["status"] != "completed"
    # Stale worker's ack is refused: job stays recoverable for the new owner.
    assert job_id in list(redis.lists[queue.processing_key])


@pytest.mark.asyncio
async def test_worker_completes_long_job_with_lease_renewal(monkeypatch):
    redis = _FakeRedis()
    queue = JobQueue(redis)
    job_id = await queue.enqueue("health_check", max_attempts=1)
    monkeypatch.setattr("brain.workers.worker.settings.WORKER_JOB_LEASE_SECONDS", 3)

    renewals = {"count": 0}
    real_renew = queue.renew_lease

    async def counting_renew(*args, **kwargs):
        renewals["count"] += 1
        return await real_renew(*args, **kwargs)

    monkeypatch.setattr(queue, "renew_lease", counting_renew)

    async def slow_job(*args, **kwargs):
        await asyncio.sleep(2.5)  # beyond a single ttl/3 renewal interval (1s)
        return {"status": "completed"}

    with patch("brain.workers.worker.execute_job", new=AsyncMock(side_effect=slow_job)):
        assert await process_one(queue) is True

    job = await queue.get_job(job_id)
    assert job["status"] == "completed"
    assert renewals["count"] >= 1


@pytest.mark.asyncio
async def test_cancel_queued_job_marks_terminal_and_releases_claim():
    redis = _FakeRedis()
    queue = JobQueue(redis)
    job_id = await queue.enqueue("reindex", idempotency_key="repo:/app")
    assert await queue.cancel(job_id)
    job = await queue.get_job(job_id)
    assert job["status"] == "cancelled"
    assert job["finished_at"]
    assert job_id not in await redis.lrange(queue.queue_key, 0, -1)
    # The released claim lets the same work be submitted again immediately.
    retry_id = await queue.enqueue("reindex", idempotency_key="repo:/app")
    assert retry_id != job_id


@pytest.mark.asyncio
async def test_cancel_non_queued_job_is_refused():
    redis = _FakeRedis()
    queue = JobQueue(redis)
    job_id = await queue.enqueue("reindex")
    await queue.update_status(job_id, JobStatus.COMPLETED)
    assert not await queue.cancel(job_id)
    assert (await queue.get_job(job_id))["status"] == "completed"


@pytest.mark.asyncio
async def test_cancel_dequeued_job_is_refused_before_status_transition():
    redis = _FakeRedis()
    queue = JobQueue(redis)
    job_id = await queue.enqueue("reindex", idempotency_key="repo:/app")
    dequeued_id, _ = await queue.dequeue(timeout=1)
    assert dequeued_id == job_id
    assert (await queue.get_job(job_id))["status"] == "queued"
    assert not await queue.cancel(job_id)
    assert job_id in await redis.lrange(queue.processing_key, 0, -1)
    assert await queue.enqueue("reindex", idempotency_key="repo:/app") == job_id


@pytest.mark.asyncio
async def test_cancel_refuses_duplicate_pending_delivery_of_dequeued_job():
    redis = _FakeRedis()
    queue = JobQueue(redis)
    job_id = await queue.enqueue("reindex")
    await queue.dequeue(timeout=1)
    await redis.lpush(queue.queue_key, job_id)
    assert not await queue.cancel(job_id)
    assert (await queue.get_job(job_id))["status"] == "queued"


@pytest.mark.asyncio
async def test_cancel_unknown_job_is_false():
    redis = _FakeRedis()
    queue = JobQueue(redis)
    assert not await queue.cancel("missing-job")


@pytest.mark.asyncio
async def test_worker_skips_a_job_cancelled_after_dequeue(monkeypatch):
    """Race: cancel lands between dequeue and the worker's status check — the
    delivery is acked away, never executed."""
    redis = _FakeRedis()
    queue = JobQueue(redis)
    job_id = await queue.enqueue("reindex")
    # Cancel lands between enqueue and the worker's status read — process_one
    # dequeues it, sees the terminal status, acks it away and never executes.
    redis.hashes[queue._job_key(job_id)]["status"] = "cancelled"
    assert await process_one(queue) is True
    assert job_id not in await redis.lrange(queue.processing_key, 0, -1)
