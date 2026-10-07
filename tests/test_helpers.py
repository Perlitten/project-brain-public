import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


def test_recent_brain_jobs_filters_before_result_limit():
    from apps.api import helpers

    ids = [f"noise-{index}" for index in range(1001)] + ["wanted"]

    class Queue:
        def __init__(self, *_args, **_kwargs):
            pass

        async def recent_job_ids(self, *, limit, offset=0):
            return ids[offset : offset + limit]

        async def get_job(self, job_id):
            params = (
                {"repository_id": 7}
                if job_id == "wanted"
                else {} if job_id == "noise-0" else {"repository_id": 8}
            )
            return {"id": job_id, "params": params, "created_at": job_id}

    with (
        patch("brain.database.session.redis_client", SimpleNamespace()),
        patch.object(helpers, "settings", SimpleNamespace(WORKER_REDIS_PREFIX="brain:worker")),
        patch("brain.workers.queue.JobQueue", Queue),
    ):
        result = asyncio.run(helpers.get_recent_brain_jobs(limit=1, repository_id=7))

    assert [job["id"] for job in result["jobs"]] == ["wanted"]
