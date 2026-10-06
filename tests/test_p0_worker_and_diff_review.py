"""Regression tests for reindex/diff-review wiring. No Postgres or Redis required."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

with (
    patch("sqlalchemy.ext.asyncio.create_async_engine"),
    patch("redis.asyncio.from_url"),
    patch("neo4j.AsyncGraphDatabase.driver"),
):
    from apps.api.main import app

from fastapi.testclient import TestClient

from brain.workers import tasks

client = TestClient(app, raise_server_exceptions=False)
HEADERS = {"X-API-Key": "test-secret-key"}


@pytest.fixture
def api_key_env():
    with patch("apps.api.auth.settings") as mock_settings:
        mock_settings.PROJECT_BRAIN_API_KEY = "test-secret-key"
        yield mock_settings


class _FakeQueue:
    """In-memory stand-in: enqueue() mints the id, get_job() reads by that id."""

    def __init__(self):
        self.jobs: dict[str, dict] = {}

    async def enqueue(self, job_type, params=None, **_kwargs):
        job_id = f"queue-minted-{len(self.jobs) + 1}"
        self.jobs[job_id] = {"id": job_id, "type": job_type, "status": "queued", "params": params}
        return job_id

    async def get_job(self, job_id):
        return self.jobs.get(job_id)


@pytest.fixture
def fake_queue():
    queue = _FakeQueue()
    with patch("brain.workers.queue.queue_for_job", return_value=queue):
        yield queue


@pytest.mark.asyncio
async def test_run_reindex_calls_index_repository_and_returns_result(tmp_path):
    repo = SimpleNamespace(id=7, path=tmp_path.as_posix(), last_indexed_commit="abc123")
    indexer = MagicMock()
    indexer.index_repository = AsyncMock(return_value=repo)
    indexer.verification = {"status": "ok"}
    with (
        patch.object(tasks, "init_db", new=AsyncMock()),
        patch.object(tasks, "FileIndexer", return_value=indexer),
    ):
        result = await tasks.run_reindex({"repo_path": tmp_path.as_posix()})

    indexer.index_repository.assert_awaited_once()
    assert indexer.index_repository.await_args.kwargs == {"clean": False, "expected_revision": ""}
    assert result["repo_id"] == 7
    assert result["commit_hash"] == "abc123"
    assert result["status"] == "ok"


@pytest.mark.asyncio
async def test_run_diff_review_imports_cleanly_and_delegates(tmp_path):
    analyzer = MagicMock()
    analyzer.review_diff = AsyncMock(return_value={"status": "completed", "report": "ok"})
    with (
        patch.object(tasks, "init_db", new=AsyncMock()),
        patch("brain.analyzers.diff_analyzer.DiffAnalyzer", return_value=analyzer) as analyzer_cls,
    ):
        result = await tasks.run_diff_review({"repo_path": tmp_path.as_posix(), "base": "main", "head": "current"})

    assert result == {"status": "completed", "report": "ok"}
    analyzer_cls.assert_called_once_with(tmp_path.resolve())
    analyzer.review_diff.assert_awaited_once_with(base="main", head="current")


def test_diff_review_poll_uses_the_id_minted_by_enqueue(api_key_env, fake_queue):
    response = client.post("/diff-review", json={"repo_path": ".", "head": "current"}, headers=HEADERS)
    assert response.status_code == 200, response.text
    job_id = response.json()["job_id"]
    assert job_id in fake_queue.jobs
    assert fake_queue.jobs[job_id]["type"] == "diff_review"

    pending = client.get(f"/diff-review/{job_id}", headers=HEADERS)
    assert pending.status_code == 200
    assert pending.json()["status"] == "queued"

    fake_queue.jobs[job_id].update(status="completed", result={"report": "clean"})
    done = client.get(f"/diff-review/{job_id}", headers=HEADERS)
    assert done.status_code == 200
    assert done.json() == {"status": "completed", "result": {"report": "clean"}}


def test_diff_review_poll_rejects_unknown_and_foreign_jobs(api_key_env, fake_queue):
    assert client.get("/diff-review/does-not-exist", headers=HEADERS).status_code == 404
    fake_queue.jobs["other"] = {"id": "other", "type": "reindex", "status": "completed"}
    assert client.get("/diff-review/other", headers=HEADERS).status_code == 404


@pytest.mark.asyncio
async def test_remote_mcp_get_diff_review_polls_the_job():
    from apps.mcp_server import remote_server

    tools = {t.name for t in await remote_server.mcp.list_tools()}
    assert {"review_diff", "get_diff_review"} <= tools

    get_mock = AsyncMock(return_value={"status": "completed", "result": {}})
    with patch.object(remote_server, "_get", get_mock):
        result = await remote_server.get_diff_review("job-1")
    assert result == {"status": "completed", "result": {}}
    get_mock.assert_awaited_once_with("/diff-review/job-1")


@pytest.mark.asyncio
async def test_remote_mcp_get_diff_review_url_quotes_job_id():
    from apps.mcp_server import remote_server

    get_mock = AsyncMock(return_value={})
    with patch.object(remote_server, "_get", get_mock):
        await remote_server.get_diff_review("../jobs/x?y=1")
    get_mock.assert_awaited_once_with("/diff-review/..%2Fjobs%2Fx%3Fy%3D1")


@pytest.mark.asyncio
async def test_execute_job_dispatches_reindex_and_diff_review_to_their_handlers():
    reindex = AsyncMock(return_value={"handler": "reindex"})
    diff_review = AsyncMock(return_value={"handler": "diff_review"})
    with patch.object(tasks, "run_reindex", reindex), patch.object(tasks, "run_diff_review", diff_review):
        assert await tasks.execute_job("reindex", {"repo_path": "a"}) == {"handler": "reindex"}
        assert await tasks.execute_job("diff_review", {"repo_path": "b"}) == {"handler": "diff_review"}
    reindex.assert_awaited_once_with({"repo_path": "a"})
    diff_review.assert_awaited_once_with({"repo_path": "b"})


def test_diff_review_runs_in_the_deep_pool():
    from brain.workers.queue import worker_pool_for_job

    assert worker_pool_for_job("diff_review") == "deep"
    assert worker_pool_for_job("reindex") == "maintenance"
