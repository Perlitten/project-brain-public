"""Operational budgets: request-body cap and worker queue depth limit."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

with (
    patch("sqlalchemy.ext.asyncio.create_async_engine"),
    patch("redis.asyncio.from_url"),
    patch("neo4j.AsyncGraphDatabase.driver"),
):
    from apps.api.main import app

from fastapi.testclient import TestClient

from brain.config.settings import settings
from brain.workers.queue import JobQueue, QueueDepthExceeded
from tests.test_worker_queue import _FakeRedis

client = TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def api_key_env():
    with patch("apps.api.auth.settings") as mock_settings:
        mock_settings.PROJECT_BRAIN_API_KEY = "test-secret-key"
        yield mock_settings


def test_request_body_limit_rejects_oversized(monkeypatch, api_key_env):
    monkeypatch.setattr(settings, "API_MAX_REQUEST_BODY_BYTES", 8)
    response = client.post(
        "/jobs/health-check",
        json={"repo_path": "/some/repository/path"},
        headers={"X-API-Key": "test-secret-key"},
    )
    assert response.status_code == 413
    assert "exceeds" in response.json()["detail"]


def test_request_body_limit_allows_under(monkeypatch, api_key_env):
    monkeypatch.setattr(settings, "API_MAX_REQUEST_BODY_BYTES", 1024 * 1024)
    response = client.post("/jobs/health-check", json={})
    # Auth rejects before enqueue; the point is the request passed the size cap.
    assert response.status_code == 401


def test_request_body_limit_disabled(monkeypatch, api_key_env):
    monkeypatch.setattr(settings, "API_MAX_REQUEST_BODY_BYTES", 0)
    response = client.post(
        "/jobs/health-check",
        json={"repo_path": "x" * 4096},
        headers={"X-API-Key": "test-secret-key"},
    )
    assert response.status_code != 413


@pytest.mark.asyncio
async def test_queue_depth_budget_rejects_enqueue(monkeypatch):
    monkeypatch.setattr(settings, "WORKER_QUEUE_MAX_DEPTH", 1)
    queue = JobQueue(_FakeRedis())
    first_id = await queue.enqueue("health_check")
    assert first_id
    with pytest.raises(QueueDepthExceeded) as exc_info:
        await queue.enqueue("health_check")
    assert exc_info.value.limit == 1
    assert exc_info.value.depth == 1


@pytest.mark.asyncio
async def test_queue_depth_idempotent_enqueue_exempt(monkeypatch):
    monkeypatch.setattr(settings, "WORKER_QUEUE_MAX_DEPTH", 1)
    queue = JobQueue(_FakeRedis())
    job_id = await queue.enqueue("health_check", idempotency_key="same-key")
    # A full queue must still resolve an idempotent retry to the same job.
    assert await queue.enqueue("health_check", idempotency_key="same-key") == job_id


@pytest.mark.asyncio
async def test_queue_depth_disabled(monkeypatch):
    monkeypatch.setattr(settings, "WORKER_QUEUE_MAX_DEPTH", 0)
    queue = JobQueue(_FakeRedis())
    for _ in range(5):
        assert await queue.enqueue("health_check")


@pytest.mark.asyncio
async def test_queue_depth_rejection_releases_idempotency_claim(monkeypatch):
    monkeypatch.setattr(settings, "WORKER_QUEUE_MAX_DEPTH", 1)
    redis = _FakeRedis()
    queue = JobQueue(redis)
    await queue.enqueue("health_check")
    with pytest.raises(QueueDepthExceeded):
        await queue.enqueue("health_check", idempotency_key="retry-after-capacity")
    assert not any(":idempotency:" in key for key in redis.values)
    assert len(redis.hashes) == 1
    assert len(redis.sorted_sets[queue.index_key]) == 1
    # Once capacity is available, the same request creates a real job.
    redis.lists[queue.queue_key].clear()
    job_id = await queue.enqueue("health_check", idempotency_key="retry-after-capacity")
    assert (await queue.get_job(job_id))["status"] == "queued"


@pytest.mark.asyncio
async def test_queue_depth_budget_holds_under_concurrent_enqueue(monkeypatch):
    monkeypatch.setattr(settings, "WORKER_QUEUE_MAX_DEPTH", 3)

    class YieldingRedis(_FakeRedis):
        def pipeline(self):
            pipe = super().pipeline()
            execute = pipe.execute

            async def yielding_execute():
                result = await execute()
                await asyncio.sleep(0)
                return result

            pipe.execute = yielding_execute
            return pipe

        async def eval(self, *args):
            await asyncio.sleep(0)
            return await super().eval(*args)

    queue = JobQueue(YieldingRedis())
    results = await asyncio.gather(
        *(queue.enqueue("health_check") for _ in range(20)),
        return_exceptions=True,
    )
    assert sum(isinstance(result, str) for result in results) == 3
    assert sum(isinstance(result, QueueDepthExceeded) for result in results) == 17
    assert (await queue.get_stats())["queued"] == 3


@pytest.mark.asyncio
async def test_queue_depth_includes_processing_and_retrying(monkeypatch):
    monkeypatch.setattr(settings, "WORKER_QUEUE_MAX_DEPTH", 2)
    redis = _FakeRedis()
    queue = JobQueue(redis)
    await redis.lpush(queue.processing_key, "running")
    await redis.zadd(queue.retry_key, {"retrying": 1.0})
    with pytest.raises(QueueDepthExceeded) as exc_info:
        await queue.enqueue("health_check")
    assert exc_info.value.depth == 2


def test_queue_depth_exceeded_maps_to_429(monkeypatch, api_key_env):
    queue = MagicMock()
    queue.enqueue = AsyncMock(side_effect=QueueDepthExceeded(5000, 5000))
    with patch("apps.api.routers.jobs.JobQueue", return_value=queue):
        response = client.post(
            "/jobs/health-check",
            json={},
            headers={"X-API-Key": "test-secret-key"},
        )
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "30"
    assert response.json()["limit"] == 5000
