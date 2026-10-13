"""SQLite maintenance queue with expiring ownership and durable DB fencing."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
import importlib
from contextlib import asynccontextmanager
from pathlib import Path

from brain.config.settings import settings
from brain.database.lite_cache import SQLiteCache


@asynccontextmanager
async def repository_lock(path: str):
    directory = Path(settings.BRAIN_LITE_DIR).expanduser() / "locks"
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / (hashlib.sha256(path.encode()).hexdigest() + ".lock")
    with lock_path.open("a+b") as stream:
        stream.write(b"0")
        stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                msvcrt = importlib.import_module("msvcrt")
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("Repository is already being indexed or repaired") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class SQLiteJobQueue:
    SUPPORTED = {"reindex", "enrich_summaries", "health_check", "embedding_verify", "embedding_backfill"}

    def __init__(self, redis: SQLiteCache, prefix: str = "brain:worker"):
        self.redis = redis
        self.prefix = prefix
        self.heartbeat_key = prefix + ":heartbeat"
        self.pool_name = "maintenance"
        with redis.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, prefix TEXT NOT NULL, idempotency TEXT,
                payload TEXT NOT NULL, status TEXT NOT NULL, token TEXT,
                expires REAL, created REAL NOT NULL, UNIQUE(prefix,idempotency))""")

    async def enqueue(self, job_type, params=None, *, idempotency_key=None, max_attempts=1, timeout_seconds=None):
        if job_type not in self.SUPPORTED:
            raise ValueError(f"Lite maintenance does not support {job_type}; use full mode")
        def write():
            from brain.workers.queue import QueueDepthExceeded
            with self.redis.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                identity = f"{job_type}:{idempotency_key}" if idempotency_key else None
                if identity:
                    existing = db.execute("SELECT id FROM jobs WHERE prefix=? AND idempotency=?", (self.prefix, identity)).fetchone()
                    if existing:
                        return existing[0]
                depth = db.execute("SELECT count(*) FROM jobs WHERE prefix=? AND status IN ('queued','running')", (self.prefix,)).fetchone()[0]
                if settings.WORKER_QUEUE_MAX_DEPTH > 0 and depth >= settings.WORKER_QUEUE_MAX_DEPTH:
                    raise QueueDepthExceeded("Lite maintenance queue depth exceeded")
                job_id = str(uuid.uuid4())
                payload = {"job_id": job_id, "type": job_type, "job_type": job_type, "params": params or {},
                           "attempt": 0, "max_attempts": max(1, max_attempts), "status": "queued",
                           "timeout_seconds": min(timeout_seconds or settings.WORKER_JOB_TIMEOUT_SECONDS,
                                                  settings.WORKER_MAX_JOB_TIMEOUT_SECONDS)}
                db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?)", (job_id,self.prefix,identity,json.dumps(payload),"queued",None,None,time.time()))
                return job_id
        return await asyncio.to_thread(write)

    async def claim(self):
        def write():
            with self.redis.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("UPDATE jobs SET status='queued',token=NULL WHERE prefix=? AND status='running' AND expires<=?", (self.prefix,time.time()))
                row = db.execute("SELECT id,payload FROM jobs WHERE prefix=? AND status='queued' ORDER BY created,id LIMIT 1", (self.prefix,)).fetchone()
                if not row:
                    return None
                payload = json.loads(row[1])
                payload["attempt"] += 1
                token = str(uuid.uuid4())
                db.execute("UPDATE jobs SET status='running',token=?,expires=?,payload=? WHERE id=?",
                           (token,time.time()+60,json.dumps(payload),row[0]))
                return payload, token
        return await asyncio.to_thread(write)

    async def renew(self, job_id, token):
        def write():
            with self.redis.connection() as db:
                return bool(db.execute("UPDATE jobs SET expires=? WHERE id=? AND token=? AND status='running' AND expires>?",
                                       (time.time()+60,job_id,token,time.time())).rowcount)
        return await asyncio.to_thread(write)

    async def finish(self, job_id, token, status, result):
        def write():
            with self.redis.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT payload FROM jobs WHERE id=? AND token=? AND status='running' AND expires>?", (job_id,token,time.time())).fetchone()
                if not row:
                    return False
                payload = json.loads(row[0])
                payload.update(status=status, result=result)
                db.execute("UPDATE jobs SET status=?,payload=?,token=NULL,expires=NULL WHERE id=?", (status,json.dumps(payload,default=str),job_id))
                return True
        return await asyncio.to_thread(write)

    async def get_job(self, job_id):
        def read():
            with self.redis.connection() as db:
                row = db.execute("SELECT payload,status FROM jobs WHERE id=? AND prefix=?", (job_id,self.prefix)).fetchone()
            return {**json.loads(row[0]), "status": row[1]} if row else None
        return await asyncio.to_thread(read)

    async def get_capacity_stats(self):
        def read():
            with self.redis.connection() as db:
                return dict(db.execute("SELECT status,count(*) FROM jobs WHERE prefix=? GROUP BY status", (self.prefix,)))
        counts = await asyncio.to_thread(read)
        return {"queued": counts.get("queued",0), "processing": counts.get("running",0), "retrying": 0}


async def serve_worker():
    """One local maintenance worker, owned by the API lifespan."""
    from brain.database.session import async_session_factory, redis_client
    from brain.workers.db_fencing import DbFence, current_job_fence, claim_db_lease, renew_db_lease, release_db_lease
    from brain.workers.errors import StaleWorkerFencingError
    from brain.workers.runtime import JobRuntime, current_job
    from brain.workers.tasks import execute_job

    queue = SQLiteJobQueue(redis_client, settings.WORKER_REDIS_PREFIX)
    worker_id = "lite:" + str(uuid.uuid4())
    while True:
        await redis_client.set(queue.heartbeat_key, json.dumps({"available_capacity":1,"worker_id":worker_id}), ex=45)
        claimed = await queue.claim()
        if claimed is None:
            await asyncio.sleep(0.5)
            continue
        job, token = claimed
        job_id = job["job_id"]
        async with async_session_factory() as session, session.begin():
            acquired = await claim_db_lease(session, job_id=job_id, fencing_token=token, worker_id=worker_id, ttl_seconds=60)
        if not acquired:
            continue
        owner = asyncio.current_task()

        async def renew():
            while True:
                await asyncio.sleep(10)
                good = await queue.renew(job_id, token)
                async with async_session_factory() as session, session.begin():
                    good = good and await renew_db_lease(session, job_id=job_id, fencing_token=token, ttl_seconds=60)
                if not good:
                    if owner:
                        owner.cancel()
                    return
                await redis_client.set(queue.heartbeat_key, json.dumps({"available_capacity":1,"worker_id":worker_id}), ex=45)

        async def check():
            if not await queue.renew(job_id, token):
                raise StaleWorkerFencingError("Lite job ownership expired")

        async def report(progress):
            await check()

        async def repair(params):
            return await queue.enqueue("embedding_backfill", params)

        fence_ctx = current_job_fence.set(DbFence(job_id, token))
        runtime_ctx = current_job.set(JobRuntime(job_id, report, check, repair))
        heartbeat = asyncio.create_task(renew())
        try:
            result = await asyncio.wait_for(execute_job(job["type"], job["params"]), job["timeout_seconds"])
            await queue.finish(job_id, token, "completed", result)
        except Exception as exc:
            await queue.finish(job_id, token, "failed", {"error": type(exc).__name__})
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            current_job.reset(runtime_ctx)
            current_job_fence.reset(fence_ctx)
            async with async_session_factory() as session, session.begin():
                await release_db_lease(session, job_id=job_id, fencing_token=token)
