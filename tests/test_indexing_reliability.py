"""Failure/replay contracts for issue #39, including real PostgreSQL commits."""

import asyncio
import os
import subprocess
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from brain.config.settings import settings
from brain.database.models import Base, File, FileChunk, IndexingRun, Repository
from brain.indexers.file_indexer import FileIndexer, MAX_CHUNK_CHARS
from brain.indexers.reliability import bounded_map, embed_chunks, repository_index_lock, git_source_is_clean
from brain.llm.providers.mock_provider import MockEmbeddingProvider
from brain.llm.providers.nvidia_provider import NvidiaEmbeddingProvider
from brain.workers.runtime import JobRuntime, current_job
from brain.workers.tasks import run_reindex, run_embedding_backfill


def test_clean_git_source_rejects_dirty_and_ignored_indexed_files(tmp_path):
    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init")
    source = tmp_path / "service.py"
    source.write_text("value = 1\n")
    (tmp_path / ".gitignore").write_text("extra.py\n")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "source")
    assert git_source_is_clean(tmp_path, [source])
    source.write_text("value = 2\n")
    assert not git_source_is_clean(tmp_path, [source])
    git("restore", "service.py")
    extra = tmp_path / "extra.py"
    extra.write_text("value = 3\n")
    assert not git_source_is_clean(tmp_path, [source, extra])


def test_clean_git_source_without_git_binary(tmp_path, monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr("brain.indexers.reliability.subprocess.run", missing)
    source = tmp_path / "service.py"
    source.write_text("value = 1\n")
    assert git_source_is_clean(tmp_path, [source])  # snapshot: nothing to verify
    (tmp_path / ".git").mkdir()
    assert not git_source_is_clean(tmp_path, [source])  # checkout we cannot verify


@pytest.mark.asyncio
async def test_progress_heartbeat_retries_transport_errors(monkeypatch):
    indexer = FileIndexer()
    publish = AsyncMock(side_effect=[RuntimeError("database unavailable"), asyncio.CancelledError()])
    monkeypatch.setattr(indexer, "_publish_progress", publish)
    monkeypatch.setattr("brain.indexers.file_indexer.asyncio.sleep", AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        await indexer._progress_heartbeat()
    assert publish.await_count == 2


@pytest.mark.asyncio
async def test_bounded_work_cancels_children_on_failure():
    active = maximum = 0
    entered = asyncio.Event()

    async def process(item):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        if active == 3:
            entered.set()
        try:
            await entered.wait()
            if item == 0:
                raise RuntimeError("crash")
            await asyncio.sleep(60)
        finally:
            active -= 1

    with pytest.raises(RuntimeError, match="crash"):
        await bounded_map(range(1000), process, 3)
    assert maximum == 3
    assert active == 0


@pytest.mark.asyncio
async def test_partial_batch_never_reassigns_another_chunks_vector():
    provider = SimpleNamespace(
        embed_batch=AsyncMock(return_value=[[99.0]]),
        embed=AsyncMock(side_effect=[[1.0], RuntimeError("500"), [3.0]]),
    )
    failures = []
    chunks = [{"content": str(i), "chunk_index": i} for i in range(3)]
    vectors = await embed_chunks(provider, chunks, asyncio.Semaphore(1),
                                lambda phase, exc, idx: failures.append((phase, idx)))
    assert vectors == [[1.0], [], [3.0]]
    assert ("embedding", 1) in failures
    assert provider.embed.await_count == 3


@pytest.mark.parametrize("status", [500, 429])
@pytest.mark.asyncio
async def test_nvidia_retry_preserves_order_and_retry_after(monkeypatch, status):
    request = httpx.Request("POST", "https://example.test/embeddings")
    client = AsyncMock()
    client.post.side_effect = [
        httpx.Response(status, headers={"Retry-After": "4"}, request=request),
        httpx.Response(200, json={"data": [{"index": 1, "embedding": [2]},
                                           {"index": 0, "embedding": [1]}]}, request=request),
    ]
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr("brain.llm.providers.nvidia_provider.httpx.AsyncClient", lambda: context)
    sleep = AsyncMock()
    monkeypatch.setattr("brain.llm.providers.nvidia_provider.asyncio.sleep", sleep)
    provider = NvidiaEmbeddingProvider(api_key="test-only")
    provider.dimension = 1  # toy vectors; the width check itself is covered in test_openai_compatible
    assert await provider.embed_batch(["a", "b"]) == [[1], [2]]
    sleep.assert_awaited_once_with(4.0)


@pytest_asyncio.fixture
async def indexing_db(monkeypatch, tmp_path):
    engine = create_async_engine(settings.DATABASE_URL)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await connection.run_sync(Base.metadata.create_all)
    except (OSError, ConnectionError):
        await engine.dispose()
        if os.environ.get("GITHUB_ACTIONS"):
            raise
        pytest.skip("PostgreSQL is unavailable locally")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("brain.indexers.file_indexer.async_session_factory", sessions)
    monkeypatch.setattr("brain.indexers.reliability.async_engine", engine)
    monkeypatch.setattr("brain.workers.tasks.async_session_factory", sessions)
    monkeypatch.setenv("FAST_INDEX", "true")
    monkeypatch.setattr(settings, "DEFAULT_EMBEDDING_PROVIDER", "mock")
    monkeypatch.setattr(settings, "LATE_INTERACTION_ENABLED", False)
    graph = SimpleNamespace(create_node=AsyncMock(), create_relationship=AsyncMock(),
                            create_relationships=AsyncMock(), delete_repository_files_not_in=AsyncMock())
    monkeypatch.setattr("brain.graph.graph_client.GraphClient", lambda **kwargs: graph)
    verification = AsyncMock(return_value={"pass": True})
    monkeypatch.setattr("brain.indexers.file_indexer.verify_embeddings", verification)
    yield sessions, graph, verification
    async with sessions.begin() as session:
        await session.execute(delete(Repository).where(Repository.path == tmp_path.as_posix()))
    await engine.dispose()


@pytest.mark.asyncio
async def test_unchanged_file_with_legacy_oversized_chunk_is_rebuilt(indexing_db, tmp_path):
    sessions, _, _ = indexing_db
    content = "x" * (MAX_CHUNK_CHARS + 17)
    (tmp_path / "data.jsonl").write_text(content, encoding="utf-8")
    await FileIndexer().index_repository(tmp_path)
    async with sessions.begin() as session:
        file = (await session.execute(select(File).where(File.path == "data.jsonl"))).scalar_one()
        original_hash = file.hash
        chunks = (await session.execute(select(FileChunk).where(FileChunk.file_id == file.id)
                                        .order_by(FileChunk.chunk_index))).scalars().all()
        # Reproduce a pre-fix stored window without changing the source hash.
        chunks[0].content = content

    replay = FileIndexer()
    await replay.index_repository(tmp_path)
    assert replay.progress["files"]["processed"] == 1
    async with sessions() as session:
        file = (await session.execute(select(File).where(File.path == "data.jsonl"))).scalar_one()
        assert file.hash == original_hash
        chunks = (await session.execute(select(FileChunk).where(FileChunk.file_id == file.id)
                                        .order_by(FileChunk.chunk_index))).scalars().all()
        assert "".join(c.content for c in chunks) == content
        assert all(len(c.content) <= MAX_CHUNK_CHARS for c in chunks)
    await replay.index_repository(tmp_path)
    assert replay.progress["files"]["skipped"] == 1
    assert replay.progress["files"]["processed"] == 0


@pytest.mark.asyncio
async def test_repository_lock_excludes_duplicate_runs_and_releases(indexing_db, tmp_path):
    async with repository_index_lock(tmp_path.as_posix()):
        with pytest.raises(RuntimeError, match="already being indexed"):
            async with repository_index_lock(tmp_path.as_posix()):
                pytest.fail("duplicate delivery acquired the repository")
    async with repository_index_lock(tmp_path.as_posix()):
        pass


@pytest.mark.asyncio
async def test_unchanged_file_refreshes_role_without_reembedding(indexing_db, tmp_path):
    sessions, _, _ = indexing_db
    directory = tmp_path / "eval"
    directory.mkdir()
    (directory / "profile.py").write_text("value = 1\n")
    await FileIndexer().index_repository(tmp_path)
    async with sessions.begin() as session:
        file = (await session.execute(select(File).where(File.path == "eval/profile.py"))).scalar_one()
        file.file_type = "source_code"  # Metadata from an older classifier.
        chunks = (await session.execute(select(FileChunk).where(FileChunk.file_id == file.id))).scalars().all()
        previous = [(c.id, c.content, c.embedding) for c in chunks]
    replay = FileIndexer()
    provider = MockEmbeddingProvider()
    provider.embed_batch = AsyncMock(side_effect=AssertionError("unchanged content was reembedded"))
    provider.embed = AsyncMock(side_effect=AssertionError("unchanged content was reembedded"))
    replay.router = SimpleNamespace(embedding=lambda _: provider)
    await replay.index_repository(tmp_path)
    assert replay.progress["files"]["processed"] == 0
    assert replay.progress["files"]["skipped"] == 1
    async with sessions() as session:
        file = (await session.execute(select(File).where(File.path == "eval/profile.py"))).scalar_one()
        assert file.file_type == "script"
        chunks = (await session.execute(select(FileChunk).where(FileChunk.file_id == file.id))).scalars().all()
        assert [c.id for c in chunks] == [c[0] for c in previous]
        assert [c.content for c in chunks] == [c[1] for c in previous]
        for chunk, (_, _, embedding) in zip(chunks, previous):
            assert list(chunk.embedding) == list(embedding)
    provider.embed_batch.assert_not_awaited()
    provider.embed.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_changed_file_keeps_old_projection_then_replays(indexing_db, tmp_path):
    sessions, _, _ = indexing_db
    path = tmp_path / "service.py"
    path.write_text("old = 1\n")
    await FileIndexer().index_repository(tmp_path)
    async with sessions() as session:
        old = (await session.execute(select(File).where(File.path == "service.py"))).scalar_one()
        old_id, old_hash = old.id, old.hash
    path.write_text("new = 2\n")
    entered = asyncio.Event()

    async def wait_for_crash(*args, **kwargs):
        entered.set()
        await asyncio.sleep(60)

    indexer = FileIndexer()
    indexer.router = SimpleNamespace(embedding=lambda _: SimpleNamespace(embed_batch=wait_for_crash))
    task = asyncio.create_task(indexer.index_repository(tmp_path))
    await asyncio.wait_for(entered.wait(), timeout=5)
    async with sessions() as session:
        saved = (await session.execute(select(File).where(File.id == old_id))).scalar_one()
        assert saved.hash == old_hash
        assert (await session.execute(select(FileChunk).where(FileChunk.file_id == old_id))).scalars().all()
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    replay = FileIndexer()
    await replay.index_repository(tmp_path)
    assert replay.progress["files"]["processed"] == 1
    assert replay.verification["status"] == "completed"
    await replay.index_repository(tmp_path)
    assert replay.progress["files"]["skipped"] == 1
    assert replay.progress["files"]["processed"] == 0
    async with sessions() as session:
        saved = (await session.execute(select(File).where(File.path == "service.py"))).scalar_one()
        assert saved.hash != old_hash
        chunks = (await session.execute(select(FileChunk).where(FileChunk.file_id == saved.id))).scalars().all()
        assert chunks[0].content == "new = 2"


@pytest.mark.asyncio
async def test_degraded_run_persists_progress_and_enqueues_causal_repair(indexing_db, tmp_path, monkeypatch):
    sessions, _, verification = indexing_db
    (tmp_path / "service.py").write_text("value = 1\n")
    verification.return_value = {"pass": False, "missing_embeddings": 1}
    provider = MockEmbeddingProvider()
    provider.embed_batch = AsyncMock(side_effect=RuntimeError("500"))
    provider.embed = AsyncMock(side_effect=RuntimeError("500"))
    indexer = FileIndexer()
    indexer.router = SimpleNamespace(embedding=lambda _: provider)
    monkeypatch.setattr("brain.workers.tasks.FileIndexer", lambda: indexer)
    monkeypatch.setattr("brain.workers.tasks.init_db", AsyncMock())
    report, enqueue = AsyncMock(), AsyncMock(return_value="repair-123")
    runtime_token = current_job.set(JobRuntime("parent-123", report, AsyncMock(), enqueue))
    try:
        result = await run_reindex({"repo_path": tmp_path.as_posix(), "verify_after": False})
    finally:
        current_job.reset(runtime_token)
    assert result["status"] == "degraded"
    assert result["repair_job_id"] == "repair-123"
    assert result["counts"]["chunks"]["failed"] == 1
    assert result["counts"]["provider_failures"][0]["path"] == "service.py"
    assert enqueue.await_args.args[0]["source_revision"] == result["commit_hash"]
    async with sessions() as session:
        run = (await session.execute(select(IndexingRun).where(IndexingRun.id == result["indexing_run_id"]))).scalar_one()
        assert run.status == "degraded"
        assert run.updated_at
        assert run.progress["files"] == dict(discovered=1, skipped=0, processed=1, failed=0)
        assert run.verification["repair_job_id"] == "repair-123"
    assert report.await_count >= 4


@pytest.mark.asyncio
async def test_graph_failure_never_marks_exact_commit_completed(indexing_db, tmp_path):
    sessions, graph, _ = indexing_db
    (tmp_path / "service.py").write_text("value = 1\n")
    graph.create_relationships.side_effect = RuntimeError("Neo4j unavailable")
    indexer = FileIndexer()
    repo = await indexer.index_repository(tmp_path)
    assert repo.indexing_status != "completed"
    assert repo.last_indexed_commit is None
    assert indexer.progress["graph"]["failed"] > 0
    async with sessions() as session:
        run = (await session.execute(select(IndexingRun).where(IndexingRun.id == indexer.indexing_run_id))).scalar_one()
        assert run.status == repo.indexing_status


@pytest.mark.parametrize("newer_run", [False, True])
@pytest.mark.asyncio
async def test_repair_promotes_only_its_latest_exact_run(indexing_db, tmp_path, monkeypatch, newer_run):
    sessions, _, verification = indexing_db
    (tmp_path / "service.py").write_text("value = 1\n")
    verification.return_value = {"pass": False, "missing_embeddings": 1}
    indexer = FileIndexer()
    record = await indexer.index_repository(tmp_path)
    if newer_run:
        async with sessions.begin() as session:
            session.add(IndexingRun(repository_id=record.id, commit_hash="b" * 40, status="failed"))
    monkeypatch.setattr("brain.workers.tasks.init_db", AsyncMock())
    monkeypatch.setattr("brain.workers.tasks.get_repository_by_path", AsyncMock(return_value=record))
    monkeypatch.setattr("brain.workers.tasks.backfill_embeddings", AsyncMock(return_value=SimpleNamespace(
        to_dict=lambda: {"regenerated": 1, "failed": 0})))
    monkeypatch.setattr("brain.workers.tasks.collect_embedding_inventory", AsyncMock(return_value=SimpleNamespace(
        to_dict=lambda: {"missing_embeddings": 0})))
    monkeypatch.setattr("brain.workers.tasks.verify_embeddings", AsyncMock(return_value={"pass": True}))
    result = await run_embedding_backfill({"repo_path": record.path, "repository_id": record.id,
                                          "indexing_run_id": indexer.indexing_run_id,
                                          "source_revision": record.last_indexed_commit,
                                          "causal_parent_job_id": "parent"})
    assert result["status"] == ("degraded" if newer_run else "completed")
    async with sessions() as session:
        repo = (await session.execute(select(Repository).where(Repository.id == record.id))).scalar_one()
        assert repo.indexing_status == ("degraded" if newer_run else "completed")


@pytest.mark.asyncio
async def test_source_change_during_indexing_fails_closed(indexing_db, tmp_path, monkeypatch):
    (tmp_path / "service.py").write_text("value = 1\n")
    revisions = iter(["a" * 40, "b" * 40])
    monkeypatch.setattr("brain.indexers.file_indexer.get_git_commit_hash", lambda _: next(revisions))
    repo = await FileIndexer().index_repository(tmp_path, expected_revision="a" * 40)
    assert repo.indexing_status == "failed"


@pytest.mark.asyncio
async def test_missing_source_never_cleans_existing_index(monkeypatch, tmp_path):
    clear = AsyncMock()
    monkeypatch.setattr("brain.indexers.file_indexer.clear_repository_index", clear)
    with pytest.raises(ValueError, match="source directory"):
        await FileIndexer().index_repository(tmp_path / "absent", clean=True)
    clear.assert_not_awaited()
