"""Attempt exhaustion and backpressure use real durable SQLite transactions."""
import asyncio
import json
import os
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

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
async def test_unexecuted_delivery_refunds_attempt_without_refunding_stale_owner(tmp_path):
    queue = SQLiteJobQueue(SQLiteCache(tmp_path), prefix="owned")
    job_id = await queue.enqueue("health_check", max_attempts=1)
    _, token = await queue.claim()
    assert not await queue.return_unexecuted_claim(job_id, "stale-token")
    assert await queue.return_unexecuted_claim(job_id, token)
    job, next_token = await queue.claim()
    assert job["attempt"] == 1
    assert not await queue.return_unexecuted_claim(job_id, token)
    assert await queue.finish(job_id, next_token, "completed", {"ok": True})


@pytest.mark.asyncio
async def test_worker_refunds_denied_fence_and_renews_short_configured_lease(tmp_path, monkeypatch):
    from brain.database import session
    from brain.workers import db_fencing, tasks
    from brain.workers.lite_queue import serve_worker
    cache = SQLiteCache(tmp_path)
    monkeypatch.setattr(session, "redis_client", cache)
    monkeypatch.setattr(settings, "WORKER_REDIS_PREFIX", "lease-fixture")
    monkeypatch.setattr(settings, "WORKER_JOB_LEASE_SECONDS", 1)

    class FakeSession:
        @asynccontextmanager
        async def begin(self):
            yield

    @asynccontextmanager
    async def factory():
        yield FakeSession()

    monkeypatch.setattr(session, "async_session_factory", factory)
    claim = AsyncMock(side_effect=[False, True])
    renewed = asyncio.Event()

    async def renew_lease(*args, **kwargs):
        if renew.await_count >= 3:
            renewed.set()
        return True

    renew = AsyncMock(side_effect=renew_lease)
    monkeypatch.setattr(db_fencing, "claim_db_lease", claim)
    monkeypatch.setattr(db_fencing, "renew_db_lease", renew)
    monkeypatch.setattr(db_fencing, "release_db_lease", AsyncMock())

    async def execute(*args):
        await asyncio.wait_for(renewed.wait(), timeout=5)
        return {"ok": True}

    executor = AsyncMock(side_effect=execute)
    monkeypatch.setattr(tasks, "execute_job", executor)
    queue = SQLiteJobQueue(cache, prefix="lease-fixture")
    job_id = await queue.enqueue("health_check", max_attempts=1)
    worker = asyncio.create_task(serve_worker())
    try:
        for _ in range(100):
            job = await queue.get_job(job_id)
            if job["status"] in {"completed", "failed"}:
                break
            await asyncio.sleep(.04)
        assert job["status"] == "completed" and job["attempt"] == 1
        executor.assert_awaited_once()
        assert claim.await_count == 2 and renew.await_count >= 3
        assert all(call.kwargs["ttl_seconds"] == 1 for call in claim.await_args_list + renew.await_args_list)
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


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
    runtime_vars = ("PATH", "SYSTEMROOT", "WINDIR", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
                    "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "TMP", "TEMP", "VIRTUAL_ENV", "PYTHONUTF8")
    env = {key: os.environ[key] for key in runtime_vars if key in os.environ}
    env.update(PYTHONPATH=str(Path(__file__).resolve().parents[1]),
               DEFAULT_LLM_PROVIDER="disabled", DEFAULT_EMBEDDING_PROVIDER="local")
    process = subprocess.Popen([sys.executable, "-c", code, str(tmp_path)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    try:
        line = process.stdout.readline()
        if not line:
            _, stderr = process.communicate(timeout=5)
            pytest.fail(f"Disposable Lite worker exited before creating a lease: {stderr[-2000:]}")
        row = json.loads(line)
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
