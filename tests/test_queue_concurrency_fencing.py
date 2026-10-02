import pytest
from unittest.mock import AsyncMock
from brain.workers.queue import JobQueue
from brain.workers.tasks import validate_task_fencing, StaleWorkerFencingError

@pytest.mark.asyncio
async def test_stale_worker_side_effect_rejected_by_fencing_token():
    redis_mock = AsyncMock()

    # 1. Worker A claims job-100 with fencing_token_A
    redis_mock.zrangebyscore.return_value = []
    redis_mock.brpoplpush.return_value = "job-100"

    queue = JobQueue(redis_mock, prefix="brain:worker:test")
    job_id, token_A = await queue.dequeue(worker_id="worker-A", lease_ttl=60)

    assert job_id == "job-100"
    assert token_A is not None

    # 2. Worker B claims job-100 after Worker A's lease expires
    token_B = "fencing-token-worker-B"
    # Redis now returns Worker B's active token
    redis_mock.hget.return_value = token_B.encode()

    # 3. Worker A attempts to commit database side effect with token_A
    with pytest.raises(StaleWorkerFencingError) as exc_info:
        await validate_task_fencing(redis_mock, queue.prefix, job_id, token_A)

    assert "Fencing mismatch" in str(exc_info.value)
    assert "Job lease was claimed by another worker" in str(exc_info.value)

@pytest.mark.asyncio
async def test_active_worker_fencing_validation_passes():
    redis_mock = AsyncMock()
    token_A = "token-active-worker-A"
    redis_mock.hget.return_value = token_A.encode()

    queue = JobQueue(redis_mock, prefix="brain:worker:test")
    # Validation should pass cleanly when tokens match
    await validate_task_fencing(redis_mock, queue.prefix, "job-100", token_A)
