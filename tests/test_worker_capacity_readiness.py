import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from brain.workers.queue import worker_pool_status
from brain.workers.worker import process_one


class _Redis:
    def __init__(self):
        self.values = {}

    async def get(self, key):
        return self.values.get(key)

    def pipeline(self):
        return self

    def llen(self, _key):
        return self

    def zcard(self, _key):
        return self

    async def execute(self):
        return [0, 0, 0]


@pytest.mark.asyncio
async def test_missing_worker_capacity_is_not_ready_even_when_redis_is_reachable():
    status = await worker_pool_status(_Redis(), "brain:worker", pools_enabled=True)
    assert all(not pool["ready"] for pool in status.values())

    redis = _Redis()
    for pool in ("fast", "maintenance", "deep"):
        redis.values[f"brain:worker:{pool}:heartbeat"] = json.dumps({"available_capacity": 1})
    status = await worker_pool_status(redis, "brain:worker", pools_enabled=True)
    assert all(pool["ready"] for pool in status.values())


@pytest.mark.asyncio
async def test_busy_worker_reports_no_available_capacity_until_job_finishes():
    queue = MagicMock()
    queue.dequeue = AsyncMock(return_value="job-1")
    queue.get_job = AsyncMock(return_value={"type": "health_check", "params": {}, "max_attempts": 1})
    queue.start_attempt = AsyncMock(return_value=1)
    queue.update_status = AsyncMock()
    queue.ack = AsyncMock()

    async def run_job(_kind, _params):
        assert queue.available_capacity == 0
        return {"ok": True}

    with patch("brain.workers.worker.execute_job", new=run_job):
        assert await process_one(queue) is True

    assert queue.available_capacity == 1
