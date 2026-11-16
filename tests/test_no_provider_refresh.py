"""Fail-closed bounded maintenance: real Git/SQLite, no external services."""
import asyncio
import datetime as dt
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from apps.api.schemas import ReindexJobRequest
from brain.config.settings import settings
from brain.database.models import Base, Repository, File, FileChunk, IndexingRun, Embedding
from brain.indexers.file_indexer import FileIndexer
from brain.indexers.no_provider_refresh import (
    NoProviderRefreshLimits, NoProviderRefreshRejected, validate_inventory, bound_mirror_tree,
    bounded_source_candidates, export_mirror_bounded, bounded_git_output,
)
from brain.llm.call_budget import no_provider_calls, ProviderBudgetExceeded, provider_calls_forbidden
from brain.workers.errors import PermanentJobError


def test_exact_inventory_rejects_new_missing_changed_duplicate_or_incomplete_paths():
    source = {'a.py': ('hash', True)}
    row = {'path': 'a.py', 'hash': 'hash', 'chunks': 1, 'longest': 20}
    validate_inventory(source, [row], 64000)
    for rows in ([], [row, row], [row | {'path': 'b.py'}], [row | {'hash': 'old'}],
                 [row | {'chunks': 0}], [row | {'longest': 64001}]):
        with pytest.raises(NoProviderRefreshRejected):
            validate_inventory(source, rows, 64000)


@pytest.mark.parametrize('kwargs', [{'max_files': 0}, {'max_files': 10001},
                                   {'timeout_s': True}, {'timeout_s': 601},
                                   {'max_source_bytes': 256 * 1024 * 1024 + 1}])
def test_resource_limits_fail_closed(kwargs):
    with pytest.raises(NoProviderRefreshRejected):
        NoProviderRefreshLimits(**kwargs)


def test_schema_rejects_unbounded_and_unpinned_requests():
    valid = dict(provider_call_budget=0, repo_path='/repo', source_revision='a' * 40)
    assert ReindexJobRequest(**valid).provider_call_budget == 0
    for change in ({'provider_call_budget': 1}, {'provider_call_budget': False}, {'provider_call_budget': 0.0}, {'clean': True}, {'benchmark_after': True},
                   {'source_revision': None}, {'repo_path': None}, {'max_files': 0}):
        with pytest.raises(ValueError):
            ReindexJobRequest(**(valid | change))


def test_guard_rejects_factories_and_cached_router_clients(monkeypatch):
    from brain.llm.factory import create_embedding_provider, create_llm_provider, create_summarizer_provider
    from brain.llm.router import ModelRouter, TaskKind
    router = ModelRouter()
    router._embedding = object()
    router._summarizer = object()
    with no_provider_calls():
        for call in (lambda: create_embedding_provider('mock'), lambda: create_llm_provider('mock'),
                     lambda: create_summarizer_provider('mock'), lambda: router.embedding('passage'),
                     router.summarizer, lambda: router.llm(TaskKind.SYNTHESIS),
                     lambda: router.llm_for_model('cached-model')):
            with pytest.raises(ProviderBudgetExceeded):
                call()
    assert not provider_calls_forbidden()
    assert create_embedding_provider('mock') is not None


def test_nvidia_provenance_does_not_construct_a_provider(monkeypatch):
    from brain.embeddings.config import get_embedding_config
    forbidden = Mock(side_effect=AssertionError('provider factory reached'))
    monkeypatch.setattr('brain.llm.get_embedding_provider', forbidden)
    monkeypatch.setattr(settings, 'DEFAULT_EMBEDDING_PROVIDER', 'nvidia')
    monkeypatch.setattr(settings, 'NVIDIA_EMBEDDING_MODEL', 'nvidia/nemotron-3-embed-1b')
    monkeypatch.setattr(settings, 'EMBEDDING_DIMENSION', 2048)
    with no_provider_calls():
        assert get_embedding_config().as_metadata() == {
            'provider': 'nvidia', 'model': 'nvidia/nemotron-3-embed-1b', 'dimension': 2048}
    forbidden.assert_not_called()


@pytest.mark.asyncio
async def test_direct_cached_provider_entrypoints_and_precreated_coroutines_are_denied():
    from brain.llm.providers.mock_provider import MockLLMProvider, MockEmbeddingProvider, MockSummarizerProvider
    from brain.llm.providers.nvidia_provider import NvidiaEmbeddingProvider
    llm, embedding, summarizer = MockLLMProvider(), MockEmbeddingProvider(), MockSummarizerProvider()
    calls = [llm.generate('x'), llm.generate_with_metadata('x'), embedding.embed('x'),
             embedding.embed_batch(['x']), summarizer.summarize('x')]
    with no_provider_calls():
        for call in calls:
            with pytest.raises(ProviderBudgetExceeded):
                await call
        with pytest.raises(ProviderBudgetExceeded):
            NvidiaEmbeddingProvider()


@pytest.mark.asyncio
async def test_budget_context_propagates_into_to_thread_and_resets():
    from brain.llm.call_budget import require_provider_budget
    with no_provider_calls():
        with pytest.raises(ProviderBudgetExceeded):
            await asyncio.to_thread(require_provider_budget)
    assert await asyncio.to_thread(provider_calls_forbidden) is False


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.DEVNULL, text=True).strip()


def test_real_git_mirror_budget_precedes_export(tmp_path):
    git(tmp_path, 'init', '-q')
    (tmp_path / 'a.py').write_text('pass\n')
    git(tmp_path, 'add', '.')
    git(tmp_path, '-c', 'user.name=Test', '-c', 'user.email=test@localhost', 'commit', '-qm', 'source')
    revision = git(tmp_path, 'rev-parse', 'HEAD')
    assert bound_mirror_tree(tmp_path / '.git', revision, NoProviderRefreshLimits()) == {'files': 1, 'bytes': 5}
    with pytest.raises(NoProviderRefreshRejected):
        bound_mirror_tree(tmp_path / '.git', revision, NoProviderRefreshLimits(max_source_bytes=4))
    with pytest.raises(NoProviderRefreshRejected):
        bound_mirror_tree(tmp_path / '.git', 'b' * 40, NoProviderRefreshLimits())
    target = tmp_path / 'export'
    target.mkdir()
    export_mirror_bounded(tmp_path / '.git', revision, target, NoProviderRefreshLimits())
    assert (target / 'a.py').read_text() == 'pass\n'
    assert not (target / '.brain-mirror.tar').exists()


def test_source_enumeration_is_bounded_before_eligibility_filters(tmp_path):
    for name in ('a.py', 'ignored.png'):
        (tmp_path / name).write_text('source')
    with pytest.raises(NoProviderRefreshRejected):
        bounded_source_candidates(tmp_path, NoProviderRefreshLimits(max_files=1), lambda name: False)
    with pytest.raises(NoProviderRefreshRejected):
        bounded_source_candidates(tmp_path, NoProviderRefreshLimits(max_source_bytes=1), lambda name: False)


def test_git_metadata_limit_and_timeout_reap_the_child():
    with pytest.raises(NoProviderRefreshRejected):
        bounded_git_output([sys.executable, '-c', 'print("x"*4096)'], 1, max_bytes=512)
    with pytest.raises(NoProviderRefreshRejected):
        bounded_git_output([sys.executable, '-c', 'import time; time.sleep(30)'], 0.05)


def test_export_subst_expansion_cannot_escape_the_disk_budget(tmp_path):
    git(tmp_path, 'init', '-q')
    (tmp_path / '.gitattributes').write_text('a.py export-subst\n')
    (tmp_path / 'a.py').write_text('$Format:%B$')
    git(tmp_path, 'add', '.')
    git(tmp_path, '-c', 'user.name=Test', '-c', 'user.email=test@localhost', 'commit', '-qm', 'x'*1000)
    revision = git(tmp_path, 'rev-parse', 'HEAD')
    limits = NoProviderRefreshLimits(max_source_bytes=40)
    assert bound_mirror_tree(tmp_path / '.git', revision, limits)['bytes'] <= 40
    target = tmp_path / 'export'
    target.mkdir()
    with pytest.raises(NoProviderRefreshRejected):
        export_mirror_bounded(tmp_path / '.git', revision, target, limits)
    assert list(target.iterdir()) == []


@pytest.mark.asyncio
async def test_api_carries_the_budget_and_disables_automatic_retries(monkeypatch):
    from apps.api.routers import jobs
    queue = SimpleNamespace(enqueue=AsyncMock(return_value='job'))
    monkeypatch.setattr(jobs, '_authorize_job', AsyncMock())
    monkeypatch.setattr(jobs, '_queue', lambda kind: queue)
    monkeypatch.setattr(jobs, '_initiator', lambda principal: object())
    monkeypatch.setattr(jobs, '_queued', AsyncMock(return_value={'job_id': 'job'}))
    await jobs.enqueue_reindex(ReindexJobRequest(provider_call_budget=0, repo_path='/repo', source_revision='a'*40), principal=object())
    args, kwargs = queue.enqueue.await_args
    assert args[1]['provider_call_budget'] == 0 and args[1]['max_files'] == 5000
    assert kwargs['max_attempts'] == 1
    await jobs.enqueue_reindex(ReindexJobRequest(repo_path='/repo'), principal=object())
    assert queue.enqueue.await_args.kwargs['max_attempts'] == 3


@pytest.mark.asyncio
async def test_worker_zero_budget_never_bootstraps_fetches_or_repairs(monkeypatch, tmp_path):
    from brain.workers.tasks import jobs
    record = SimpleNamespace(id=1, path=str(tmp_path), last_indexed_commit='a' * 40)
    indexer = SimpleNamespace(index_repository=AsyncMock(return_value=record),
                              verification={'status': 'completed', 'provider_call_budget': 0, 'provider_calls': 0})
    monkeypatch.setattr(jobs, 'FileIndexer', lambda **kwargs: indexer)
    init = AsyncMock(side_effect=AssertionError('DDL bootstrap reached'))
    fetch = AsyncMock(side_effect=AssertionError('source fetch reached'))
    monkeypatch.setattr(jobs, 'init_db', init)
    monkeypatch.setattr(jobs, '_prepare_source_revision', fetch)
    monkeypatch.setattr(jobs, '_recorded_mirror_plan', AsyncMock(return_value=None))
    monkeypatch.setattr(jobs, '_record_source_mode', AsyncMock())
    params = {'provider_call_budget': 0, 'repo_path': str(tmp_path), 'source_revision': 'a' * 40}
    result = await jobs.run_reindex(params)
    assert result['status'] == 'completed'
    init.assert_not_awaited()
    fetch.assert_not_awaited()
    indexer.verification['status'] = 'degraded'
    with pytest.raises(PermanentJobError):
        await jobs.run_reindex(params)
    assert not provider_calls_forbidden()


@pytest.mark.asyncio
async def test_worker_budget_timeout_cancels_and_resets_context(monkeypatch, tmp_path):
    from brain.workers.tasks import jobs
    async def delayed(*args, **kwargs):
        await asyncio.sleep(30)
    monkeypatch.setattr(jobs, '_run_reindex', delayed)
    with pytest.raises(PermanentJobError) as denied:
        await jobs.run_reindex({'provider_call_budget': 0, 'repo_path': str(tmp_path),
                               'source_revision': 'a' * 40, 'timeout_s': 1})
    assert denied.value.result['error_type'] == 'TimeoutError'
    assert not provider_calls_forbidden()


@pytest_asyncio.fixture
async def local_index(monkeypatch, tmp_path):
    # Only a throw-away SQLite database. Production data/credentials unavailable.
    engine = create_async_engine('sqlite+aiosqlite:///' + str(tmp_path / 'test.db'),
                                 execution_options={'schema_translate_map': {'harness': None}})
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr('brain.indexers.file_indexer.async_session_factory', sessions)
    monkeypatch.setattr('brain.indexers.reliability.async_engine', engine)
    monkeypatch.setattr('brain.indexers.file_indexer.assert_current_db_fence', AsyncMock())
    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def local_lock(path):
        yield
    monkeypatch.setattr('brain.indexers.file_indexer.repository_index_lock', local_lock)
    graph = SimpleNamespace(create_node=AsyncMock(), create_relationship=AsyncMock(),
                            create_relationships=AsyncMock(), delete_repository_files_not_in=AsyncMock())
    monkeypatch.setattr('brain.graph.graph_client.GraphClient', lambda **kwargs: graph)
    verify = AsyncMock(return_value={'pass': True})
    monkeypatch.setattr('brain.indexers.file_indexer.verify_embeddings', verify)
    repo = tmp_path / 'source'
    repo.mkdir()
    git(repo, 'init', '-q')
    (repo / 'a.py').write_text('def hello():\n    return 1\n')
    git(repo, 'add', '.')
    git(repo, '-c', 'user.name=Test', '-c', 'user.email=test@localhost', 'commit', '-qm', 'source')
    first = FileIndexer()
    record = await first.index_repository(repo)
    assert record.indexing_status == 'completed'
    old = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    async with sessions.begin() as session:
        await session.execute(update(Repository).where(Repository.id == record.id).values(updated_at=old))
    yield sessions, repo, record.id, git(repo, 'rev-parse', 'HEAD'), old, graph, verify
    await engine.dispose()
    # The constrained suite shares a 256MiB tmpfs. Retain assertions/receipts,
    # not eight complete temporary schemas until pytest session teardown.
    for name in ('test.db', 'test.db-wal', 'test.db-shm'):
        (tmp_path / name).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_native_unchanged_refresh_completes_without_provider_and_preserves_index(local_index, monkeypatch):
    sessions, repo, repo_id, revision, old, graph, verify = local_index
    forbidden = Mock(side_effect=AssertionError('model router reached'))
    monkeypatch.setattr('brain.indexers.file_indexer.get_model_router', forbidden)
    indexer = FileIndexer(NoProviderRefreshLimits())
    result = await indexer.index_repository(repo, expected_revision=revision)
    assert result.last_indexed_commit == revision
    assert indexer.verification['status'] == 'completed'
    assert indexer.verification['complete_file_preflight'] is True
    assert indexer.verification['provider_calls'] == 0
    assert indexer.file_counts['unchanged'] == 1 and indexer.file_counts['indexed'] == 0
    assert verify.await_count >= 3  # Seed, full preflight, native final verification.
    assert graph.create_node.await_count > 0
    forbidden.assert_not_called()
    async with sessions() as session:
        saved = await session.get(Repository, repo_id)
        assert saved.updated_at.replace(tzinfo=dt.timezone.utc) > old
        runs = (await session.scalars(select(IndexingRun).where(IndexingRun.repository_id == repo_id))).all()
        assert [run.status for run in runs] == ['completed', 'completed']


@pytest.mark.asyncio
async def test_integration_preserves_upstream_binary_exclusion_and_vector_cleanup(local_index):
    """922ebf's projection-cleanup fix must survive the narrow maintenance patch."""
    sessions, repo, repo_id, revision, old, graph, verify = local_index
    async with sessions() as session:
        assert (await session.scalars(select(Embedding))).all()
    (repo / 'a.py').write_bytes(b'\x00binary\x00')
    git(repo, 'add', '.')
    git(repo, '-c', 'user.name=Test', '-c', 'user.email=test@localhost', 'commit', '-qm', 'binary source')
    indexer = FileIndexer()
    result = await indexer.index_repository(repo)
    assert result.indexing_status == 'completed'
    assert indexer.file_counts['excluded'] == 1
    async with sessions() as session:
        assert (await session.scalars(select(File).where(File.repository_id == repo_id))).all() == []
        assert (await session.scalars(select(FileChunk))).all() == []
        assert (await session.scalars(select(Embedding))).all() == []
    assert graph.delete_repository_files_not_in.await_args.args[0] == set()


@pytest.mark.asyncio
async def test_constructor_failure_after_run_creation_is_durable_and_does_not_renew_age(local_index, monkeypatch):
    sessions, repo, repo_id, revision, old, graph, verify = local_index
    def unavailable(**kwargs):
        raise RuntimeError('graph constructor unavailable')
    monkeypatch.setattr('brain.graph.graph_client.GraphClient', unavailable)
    with pytest.raises(RuntimeError, match='graph constructor'):
        await FileIndexer(NoProviderRefreshLimits()).index_repository(repo, expected_revision=revision)
    async with sessions() as session:
        saved = await session.get(Repository, repo_id)
        assert saved.updated_at.replace(tzinfo=dt.timezone.utc) == old
        runs = (await session.scalars(select(IndexingRun).where(IndexingRun.repository_id == repo_id))).all()
        assert [run.status for run in runs] == ['completed', 'failed']
        assert runs[-1].completed_at is not None
        assert runs[-1].verification['error_type'] == 'RuntimeError'


@pytest.mark.asyncio
async def test_real_worker_timeout_after_run_creation_preserves_failed_receipt_and_age(local_index, monkeypatch):
    from brain.workers.tasks import jobs
    sessions, repo, repo_id, revision, old, graph, verify = local_index
    entered_graph = asyncio.Event()
    real_timeout = asyncio.timeout
    deadline = None
    def after_receipt_timeout(seconds):
        # Use the actual asyncio timer, but arm it only after the durable run
        # exists. Host contention before that point must not change which
        # failure boundary this fault-injection contract exercises.
        nonlocal deadline
        deadline = real_timeout(None)
        return deadline
    monkeypatch.setattr(jobs.asyncio, 'timeout', after_receipt_timeout)
    async def slow_graph(**kwargs):
        entered_graph.set()
        deadline.reschedule(asyncio.get_running_loop().time() + 0.01)
        await asyncio.sleep(30)
    graph.create_node.side_effect = slow_graph
    monkeypatch.setattr(jobs, '_recorded_mirror_plan', AsyncMock(return_value=None))
    monkeypatch.setattr(jobs, '_record_source_mode', AsyncMock())
    bootstrap = AsyncMock(side_effect=AssertionError('bootstrap reached'))
    monkeypatch.setattr(jobs, 'init_db', bootstrap)
    with pytest.raises(PermanentJobError) as denied:
        await jobs.run_reindex({'provider_call_budget': 0, 'repo_path': str(repo),
                               'source_revision': revision, 'timeout_s': 2})
    assert entered_graph.is_set()  # Timeout really followed the committed running receipt.
    assert denied.value.result['error_type'] == 'TimeoutError'
    bootstrap.assert_not_awaited()
    async with sessions() as session:
        saved = await session.get(Repository, repo_id)
        assert saved.updated_at.replace(tzinfo=dt.timezone.utc) == old
        runs = (await session.scalars(select(IndexingRun).where(IndexingRun.repository_id == repo_id))).all()
        assert [run.status for run in runs] == ['completed', 'failed']
        assert runs[-1].completed_at is not None
        assert runs[-1].verification['error_type'] == 'CancelledError'


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['new_source', 'missing_file', 'extra_indexed_file', 'missing_chunks', 'coverage', 'graph', 'race'])
async def test_failed_refresh_never_renews_age_or_prepares_provider(local_index, monkeypatch, failure):
    sessions, repo, repo_id, revision, old, graph, verify = local_index
    if failure == 'new_source':
        (repo / 'new.py').write_text('new = 1\n')
        git(repo, 'add', '.')
        git(repo, '-c', 'user.name=Test', '-c', 'user.email=test@localhost', 'commit', '-qm', 'new')
        revision = git(repo, 'rev-parse', 'HEAD')
    elif failure == 'missing_file':
        from sqlalchemy import delete
        async with sessions.begin() as session:
            await session.execute(delete(File))
    elif failure == 'extra_indexed_file':
        async with sessions.begin() as session:
            session.add(File(repository_id=repo_id, path='ghost.py', hash='old', file_type='source_code'))
    elif failure == 'missing_chunks':
        from sqlalchemy import delete
        async with sessions.begin() as session:
            await session.execute(delete(FileChunk))
    elif failure == 'coverage':
        verify.return_value = {'pass': False}
    elif failure == 'graph':
        graph.create_node.side_effect = RuntimeError('graph unavailable')
    forbidden = AsyncMock(side_effect=AssertionError('provider preparation reached'))
    indexer = FileIndexer(NoProviderRefreshLimits())
    monkeypatch.setattr(indexer, '_prepare_file', forbidden)
    if failure == 'race':
        original = indexer.index_file
        async def changed_after_source_snapshot(repo_id, source, path):
            path.write_text('changed_after_preflight = True\n')
            return await original(repo_id, source, path)
        monkeypatch.setattr(indexer, 'index_file', changed_after_source_snapshot)
    with pytest.raises(NoProviderRefreshRejected):
        await indexer.index_repository(repo, expected_revision=revision)
    forbidden.assert_not_awaited()
    async with sessions() as session:
        saved = await session.get(Repository, repo_id)
        assert saved.updated_at.replace(tzinfo=dt.timezone.utc) == old
        runs = (await session.scalars(select(IndexingRun).where(IndexingRun.repository_id == repo_id))).all()
        assert runs[0].status == 'completed'
        if failure == 'graph':
            assert runs[-1].status == 'failed'  # Failed attempt remains visible.
