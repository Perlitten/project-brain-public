import pytest
import time
from unittest.mock import AsyncMock
from brain.workers.queue import JobQueue

@pytest.mark.asyncio
async def test_queue_dequeue_claims_fencing_token():
    redis_mock = AsyncMock()
    redis_mock.zrangebyscore.return_value = []
    redis_mock.brpoplpush.return_value = "job-123"

    queue = JobQueue(redis_mock, prefix="brain:worker:test")
    job_id, fencing_token = await queue.dequeue(timeout=1, worker_id="worker-A", lease_ttl=60)

    assert job_id == "job-123"
    assert fencing_token is not None
    assert len(fencing_token) >= 8
    redis_mock.eval.assert_awaited_once()
    assert redis_mock.eval.await_args.args[1] == 2

@pytest.mark.asyncio
async def test_ack_rejects_mismatched_fencing_token():
    redis_mock = AsyncMock()
    redis_mock.hget.return_value = b"token-correct"

    queue = JobQueue(redis_mock, prefix="brain:worker:test")
    success = await queue.ack("job-123", fencing_token="token-wrong")

    assert success is False
    redis_mock.lrem.assert_not_called()

@pytest.mark.asyncio
async def test_recover_stale_ignores_active_leases():
    redis_mock = AsyncMock()
    redis_mock.lrange.return_value = [b"job-active", b"job-stale"]

    # Active lease expires in 100s; stale lease expired 10s ago
    now = time.time()
    def mock_hget(key, field):
        if "job-active" in key:
            return str(now + 100.0).encode()
        return str(now - 10.0).encode()

    redis_mock.hget.side_effect = mock_hget
    redis_mock.lrem.return_value = 1

    queue = JobQueue(redis_mock, prefix="brain:worker:test")
    moved = await queue.recover_stale(lease_buffer=5.0)

    assert moved == 1
    # Only job-stale should be removed and pushed back to queue
    redis_mock.eval.assert_awaited_once()
    assert redis_mock.eval.await_args.args[1] == 3
    assert redis_mock.eval.await_args.args[-1] == "job-stale"
