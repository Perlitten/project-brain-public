"""Attempt exhaustion and backpressure use real durable SQLite transactions."""
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from brain.config.settings import settings
from brain.database.lite_cache import SQLiteCache
from brain.workers.lite_queue import SQLiteJobQueue
from brain.workers.queue import QueueDepthExceeded


@pytest.mark.asyncio
@pytest.mark.parametrize("attempts", [1, 2])
async def test_expired_leases_allow_exactly_configured_attempts(tmp_path, attempts):
    cache = SQLiteCache(tmp_path)
    queue = SQLiteJobQueue(cache, prefix="owned")
    job_id = await queue.enqueue("health_check", max_attempts=attempts)
    for expected_attempt in range(1, attempts + 1):
        job, token = await queue.claim()
        assert job["attempt"] == expected_attempt
        with cache.connection() as db:
            db.execute("UPDATE jobs SET expires=0 WHERE id=?", (job_id,))
        assert not await queue.finish(job_id, token, "completed", {"stale": True})
        # Recreate both objects as a separate worker does after restart.
        queue = SQLiteJobQueue(SQLiteCache(tmp_path), prefix="owned")
    assert await queue.claim() is None
    final = await queue.get_job(job_id)
    assert final["status"] == "failed" and final["attempt"] == attempts
    assert final["result"] == {"error": "lease_expired"}


@pytest.mark.asyncio
async def test_concurrent_recovery_claims_one_owner(tmp_path):
    cache = SQLiteCache(tmp_path)
    queues = [SQLiteJobQueue(cache, prefix="owned") for _ in range(8)]
    job_id = await queues[0].enqueue("health_check", max_attempts=2)
    await queues[0].claim()
    with cache.connection() as db:
        db.execute("UPDATE jobs SET expires=0 WHERE id=?", (job_id,))
    claims = await asyncio.gather(*(queue.claim() for queue in queues))
    successful = [row for row in claims if row]
    assert len(successful) == 1 and successful[0][0]["attempt"] == 2


@pytest.mark.asyncio
async def test_capacity_is_typed_and_preserves_first_job(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "WORKER_QUEUE_MAX_DEPTH", 1)
    queue = SQLiteJobQueue(SQLiteCache(tmp_path), prefix="owned")
    first = await queue.enqueue("health_check")
    with pytest.raises(QueueDepthExceeded) as exc:
        await queue.enqueue("health_check")
    assert (exc.value.depth, exc.value.limit) == (1, 1)
    assert await queue.recent_job_ids() == [first]


def test_real_worker_kill_and_expired_lease_cannot_execute_poison_job_twice(tmp_path):
    code = '''
import asyncio, json, sys, time
from pathlib import Path
from brain.config.settings import settings
from brain.database.lite_cache import SQLiteCache
from brain.workers.lite_queue import SQLiteJobQueue
settings.WORKER_JOB_LEASE_SECONDS = 1
async def claim():
    queue = SQLiteJobQueue(SQLiteCache(Path(sys.argv[1])), prefix="crash-fixture")
    job_id = await queue.enqueue("health_check", max_attempts=1)
    job, token = await queue.claim()
    print(json.dumps({"job_id": job_id, "attempt": job["attempt"]}), flush=True)
asyncio.run(claim())
time.sleep(30)
'''
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
           "DEFAULT_LLM_PROVIDER": "disabled", "DEFAULT_EMBEDDING_PROVIDER": "local"}
    process = subprocess.Popen([sys.executable, "-c", code, str(tmp_path)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    try:
        row = json.loads(process.stdout.readline())
        assert row["attempt"] == 1
        process.kill()
        process.wait(timeout=5)
        # Actual monotonic passage of the configured one-second lease; expiry
        # is not injected in this crash/restart test.
        time.sleep(1.1)

        async def recover():
            queue = SQLiteJobQueue(SQLiteCache(tmp_path), prefix="crash-fixture")
            assert await queue.claim() is None
            final = await queue.get_job(row["job_id"])
            assert final["status"] == "failed" and final["attempt"] == 1

        asyncio.run(recover())
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


def test_sqlite_enqueue_at_capacity_returns_http_429(tmp_path, monkeypatch):
    from apps.api.main import queue_depth_exceeded_handler
    monkeypatch.setattr(settings, "WORKER_QUEUE_MAX_DEPTH", 1)
    queue = SQLiteJobQueue(SQLiteCache(tmp_path), prefix="owned")
    app = FastAPI()
    app.add_exception_handler(QueueDepthExceeded, queue_depth_exceeded_handler)

    @app.post("/enqueue")
    async def enqueue():
        return {"id": await queue.enqueue("health_check")}

    with TestClient(app) as client:
        assert client.post("/enqueue").status_code == 200
        response = client.post("/enqueue")
        assert response.status_code == 429 and response.headers["retry-after"] == "30"
        assert response.json()["depth"] == 1
