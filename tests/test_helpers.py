import asyncio
from types import SimpleNamespace
from unittest.mock import patch


def test_recent_brain_jobs_filters_before_result_limit():
    from apps.api import helpers

    ids = [f"noise-{index}" for index in range(1001)] + ["wanted"]

    class Queue:
        calls = []

        def __init__(self, *_args, **_kwargs):
            pass

        async def recent_job_ids(self, *, limit, offset=0):
            self.calls.append((limit, offset))
            return ids[offset : offset + limit]

        async def get_job(self, job_id):
            params = (
                {"repository_id": 7}
                if job_id == "wanted"
                else {} if job_id == "noise-0" else {"repository_id": 8}
            )
            return {"id": job_id, "params": params, "created_at": job_id}

        async def get_jobs(self, job_ids):
            return [await self.get_job(job_id) for job_id in job_ids]

    with (
        patch("brain.database.session.redis_client", SimpleNamespace()),
        patch.object(helpers, "settings", SimpleNamespace(WORKER_REDIS_PREFIX="brain:worker")),
        patch("brain.workers.queue.JobQueue", Queue),
    ):
        result = asyncio.run(helpers.get_recent_brain_jobs(limit=1, repository_id=7))

    assert [job["id"] for job in result["jobs"]] == ["wanted"]


def test_recent_brain_jobs_scoped_scan_stops_after_requested_matches():
    from apps.api import helpers

    ids = ["wanted"] + [f"noise-{index}" for index in range(1001)]

    class Queue:
        calls = []

        def __init__(self, *_args, **_kwargs):
            pass

        async def recent_job_ids(self, *, limit, offset=0):
            self.calls.append((limit, offset))
            return ids[offset : offset + limit]

        async def get_job(self, job_id):
            return {"id": job_id, "params": {"repository_id": 7 if job_id == "wanted" else 8}}

        async def get_jobs(self, job_ids):
            return [await self.get_job(job_id) for job_id in job_ids]

    with (
        patch("brain.database.session.redis_client", SimpleNamespace()),
        patch.object(helpers, "settings", SimpleNamespace(WORKER_REDIS_PREFIX="brain:worker")),
        patch("brain.workers.queue.JobQueue", Queue),
    ):
        result = asyncio.run(helpers.get_recent_brain_jobs(limit=1, repository_id=7))

    assert [job["id"] for job in result["jobs"]] == ["wanted"]
    assert Queue.calls == [(1000, 0)]


def test_recent_brain_jobs_reads_the_repository_index_instead_of_the_global_scan():
    from apps.api import helpers
    from brain.workers.queue import JobQueue
    from tests.test_worker_queue import _FakeRedis

    redis = _FakeRedis()

    async def scenario():
        queue = JobQueue(redis, prefix="brain:worker")
        wanted = await queue.enqueue("reindex", {"repository_id": 7})
        for _ in range(30):
            await queue.enqueue("reindex", {"repository_id": 8})

        async def no_global_scan(*_args, **_kwargs):
            raise AssertionError("scoped read walked the global index")

        with (
            patch("brain.database.session.redis_client", redis),
            patch.object(helpers, "settings", SimpleNamespace(WORKER_REDIS_PREFIX="brain:worker")),
            patch.object(JobQueue, "recent_job_ids", no_global_scan),
        ):
            result = await helpers.get_recent_brain_jobs(limit=5, repository_id=7)
        return wanted, result

    wanted, result = asyncio.run(scenario())

    assert result["error"] is None
    assert [job["id"] for job in result["jobs"]] == [wanted]


def test_recent_brain_jobs_pages_past_expired_scoped_payloads():
    from apps.api import helpers
    from brain.workers.queue import JobQueue
    from tests.test_worker_queue import _FakeRedis

    redis = _FakeRedis()

    async def scenario():
        queue = JobQueue(redis, prefix="brain:worker")
        wanted = await queue.enqueue("reindex", {"repository_id": 7})
        expired = [await queue.enqueue("reindex", {"repository_id": 7}) for _ in range(20)]
        for job_id in expired:
            await redis.delete(queue._job_key(job_id))

        async def no_global_scan(*_args, **_kwargs):
            raise AssertionError("scoped read walked the global index")

        with (
            patch("brain.database.session.redis_client", redis),
            patch.object(helpers, "settings", SimpleNamespace(WORKER_REDIS_PREFIX="brain:worker")),
            patch.object(JobQueue, "recent_job_ids", no_global_scan),
        ):
            result = await helpers.get_recent_brain_jobs(limit=1, repository_id=7)
        return wanted, result

    wanted, result = asyncio.run(scenario())

    assert result["error"] is None
    assert [job["id"] for job in result["jobs"]] == [wanted]
