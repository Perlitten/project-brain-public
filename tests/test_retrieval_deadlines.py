import asyncio
import time

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from sqlalchemy.exc import OperationalError

from brain.retrieval.service import RetrievalService


@pytest.mark.asyncio
async def test_known_symbol_route_is_not_dependent_on_vector_search(monkeypatch):
    service = RetrievalService()

    async def known_symbol(*_args, **_kwargs):
        from brain.retrieval.service import RetrievalCandidate
        return [RetrievalCandidate(path="brain/search/code_search.py", symbols=["search_code"], score=1.0)]

    vector_cancelled = asyncio.Event()

    async def unavailable_vector(*_args, **_kwargs):
        # A real datastore may return before the deadline, which exercises
        # channel merging instead of this test's symbol-only fallback contract.
        try:
            await asyncio.Event().wait()
        finally:
            vector_cancelled.set()

    monkeypatch.setattr(service, "_known_symbol_route", known_symbol)
    monkeypatch.setattr("brain.retrieval.service.search_code", unavailable_vector)
    result = await asyncio.wait_for(service.retrieve("search_code", None, deadline_s=0.001), 0.25)
    assert result.repository["fast_route"] == "symbol"
    assert result.candidates[0].path == "brain/search/code_search.py"
    assert result.degraded == ["deadline_exceeded"]
    assert vector_cancelled.is_set()


@pytest.mark.parametrize("search_mode", ["success", "stalled", "error"])
@pytest.mark.asyncio
async def test_stalled_symbols_cannot_extend_the_request_deadline(monkeypatch, search_mode):
    service = RetrievalService()
    stopped = asyncio.Event()

    async def symbols(*args):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async def search(*args, **kwargs):
        if search_mode == "stalled":
            await asyncio.Event().wait()
        if search_mode == "error":
            raise RuntimeError("unavailable")
        return {"files": [{"path": "src/core.py"}]}

    monkeypatch.setattr(service, "_known_symbol_route", symbols)
    monkeypatch.setattr("brain.retrieval.service.search_code", search)
    result = await asyncio.wait_for(service.retrieve("core", None, deadline_s=0.02), timeout=0.25)
    assert "deadline_exceeded" in result.degraded
    assert bool(result.candidates) == (search_mode == "success")
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_deadline_between_primary_completion_and_bm25_start_is_reported(monkeypatch):
    from brain.config.settings import settings
    monkeypatch.setattr(settings, "RETRIEVAL_V2_ENABLED", True)
    monkeypatch.setattr(settings, "DEFAULT_EMBEDDING_PROVIDER", "local")
    monkeypatch.setattr("brain.retrieval.service.get_repository_by_path", AsyncMock(return_value=SimpleNamespace(id=1)))
    monkeypatch.setattr("brain.retrieval.service.search_code", AsyncMock(return_value={"files": [{"path": "src/core.py"}]}))
    lexical = AsyncMock()
    monkeypatch.setattr("brain.search.local_lexical.local_lexical", lexical)
    service = RetrievalService()
    monkeypatch.setattr(service, "_known_symbol_route", AsyncMock(return_value=[]))
    normalizer = service._normalize
    clock = [0.0]
    monkeypatch.setattr("brain.retrieval.service.time.perf_counter", lambda: clock[0])

    def normalize(*args):
        rows = normalizer(*args)
        clock[0] = 0.05  # Primary normalization used the last available budget.
        return rows

    monkeypatch.setattr(service, "_normalize", normalize)
    result = await service.retrieve("core", "/repo", deadline_s=0.05)
    assert result.candidates[0].path == "src/core.py"
    assert result.degraded == ["deadline_exceeded", "offline_lexical_unavailable"]
    lexical.assert_not_called()


@pytest.mark.asyncio
async def test_repository_lookup_is_inside_the_deadline(monkeypatch):
    stopped = asyncio.Event()

    async def lookup(*args):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    search = AsyncMock()
    monkeypatch.setattr("brain.retrieval.service.get_repository_by_path", lookup)
    monkeypatch.setattr("brain.retrieval.service.search_code", search)
    result = await asyncio.wait_for(RetrievalService().retrieve("core", "/repo", deadline_s=0.02), 0.25)
    assert result.degraded == ["deadline_exceeded"]
    search.assert_not_called()
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_fallback_freshness_cannot_start_a_new_deadline(monkeypatch):
    repo = SimpleNamespace(id=1, path="/repo", last_indexed_commit="abc")
    monkeypatch.setattr("brain.retrieval.service.get_repository_by_path", AsyncMock(return_value=repo))
    monkeypatch.setattr("brain.retrieval.service.search_code", AsyncMock(side_effect=RuntimeError()))
    service = RetrievalService()
    from brain.retrieval.service import RetrievalCandidate
    monkeypatch.setattr(service, "_known_symbol_route", AsyncMock(return_value=[RetrievalCandidate("src/core.py")]))
    stopped = asyncio.Event()

    async def freshness(*args):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr("brain.retrieval.service.assess_repository_freshness", freshness)
    result = await asyncio.wait_for(service.retrieve("core", "/repo", deadline_s=0.02), 0.25)
    assert result.candidates[0].path == "src/core.py"
    assert result.repository["freshness"]["status"] == "unknown"
    assert "deadline_exceeded" in result.degraded
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_request_cancellation_stops_both_children(monkeypatch):
    service = RetrievalService()
    started, symbols_stopped, search_stopped = (asyncio.Event() for _ in range(3))

    async def symbols(*args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            symbols_stopped.set()

    async def search(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            search_stopped.set()

    monkeypatch.setattr(service, "_known_symbol_route", symbols)
    monkeypatch.setattr("brain.retrieval.service.search_code", search)
    request = asyncio.create_task(service.retrieve("core", None))
    await started.wait()
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert symbols_stopped.is_set() and search_stopped.is_set()


@pytest.mark.asyncio
async def test_deadline_does_not_wait_for_slow_provider_cancellation_cleanup(monkeypatch):
    service = RetrievalService()
    release, cleaned = asyncio.Event(), asyncio.Event()

    async def search(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            await release.wait()
            cleaned.set()

    monkeypatch.setattr("brain.retrieval.service.search_code", search)
    monkeypatch.setattr(service, "_known_symbol_route", AsyncMock(return_value=[]))
    try:
        # A retrieve that waited for cleanup would hang until release; 5s is load headroom, not a budget.
        result = await asyncio.wait_for(service.retrieve("core", None, deadline_s=0.02), 5)
        assert result.degraded == ["deadline_exceeded"]
        assert not cleaned.is_set()
    finally:
        release.set()
        await asyncio.wait_for(cleaned.wait(), 5)


@pytest.mark.parametrize("error", [RuntimeError("stream failed"), OperationalError("read", {}, RuntimeError("offline")), ValueError("budget"), TimeoutError()])
@pytest.mark.asyncio
async def test_optional_bm25_failure_keeps_successful_primary_channels(monkeypatch, error):
    from brain.config.settings import settings
    from brain.retrieval.service import RetrievalCandidate
    monkeypatch.setattr(settings, "RETRIEVAL_V2_ENABLED", True)
    monkeypatch.setattr(settings, "DEFAULT_EMBEDDING_PROVIDER", "local")
    monkeypatch.setattr("brain.retrieval.service.get_repository_by_path", AsyncMock(return_value=SimpleNamespace(id=1)))
    monkeypatch.setattr("brain.retrieval.service.search_code", AsyncMock(return_value={"chunks": [{"file_path": "src/core.py", "similarity": 1}]}))
    monkeypatch.setattr("brain.search.local_lexical.local_lexical", AsyncMock(side_effect=error))
    service = RetrievalService()
    monkeypatch.setattr(service, "_known_symbol_route", AsyncMock(return_value=[RetrievalCandidate("src/symbol.py", channel_scores={"symbol": 1})]))
    result = await service.retrieve("core", "/repo")
    assert {row.path for row in result.candidates} == {"src/core.py", "src/symbol.py"}
    assert result.degraded == ["offline_lexical_unavailable"]


@pytest.mark.asyncio
async def test_optional_bm25_does_not_swallow_cancellation(monkeypatch):
    from brain.config.settings import settings
    monkeypatch.setattr(settings, "RETRIEVAL_V2_ENABLED", True)
    monkeypatch.setattr(settings, "DEFAULT_EMBEDDING_PROVIDER", "local")
    monkeypatch.setattr("brain.retrieval.service.get_repository_by_path", AsyncMock(return_value=SimpleNamespace(id=1)))
    monkeypatch.setattr("brain.retrieval.service.search_code", AsyncMock(return_value={"files": [{"path": "src/core.py"}]}))
    monkeypatch.setattr("brain.search.local_lexical.local_lexical", AsyncMock(side_effect=asyncio.CancelledError()))
    service = RetrievalService()
    monkeypatch.setattr(service, "_known_symbol_route", AsyncMock(return_value=[]))
    with pytest.raises(asyncio.CancelledError):
        await service.retrieve("core", "/repo")


@pytest.mark.asyncio
async def test_bm25_uses_remaining_deadline_and_returns_primary_results(monkeypatch):
    from brain.config.settings import settings
    monkeypatch.setattr(settings, "RETRIEVAL_V2_ENABLED", True)
    monkeypatch.setattr(settings, "DEFAULT_EMBEDDING_PROVIDER", "local")
    monkeypatch.setattr("brain.retrieval.service.get_repository_by_path", AsyncMock(return_value=SimpleNamespace(id=1)))
    monkeypatch.setattr("brain.retrieval.service.search_code", AsyncMock(return_value={"files": [{"path": "src/core.py"}]}))
    stopped = asyncio.Event()

    async def lexical(*args):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr("brain.search.local_lexical.local_lexical", lexical)
    service = RetrievalService()
    monkeypatch.setattr(service, "_known_symbol_route", AsyncMock(return_value=[]))
    result = await asyncio.wait_for(service.retrieve("core", "/repo", deadline_s=0.02), 0.25)
    assert result.candidates[0].path == "src/core.py"
    assert result.degraded == ["deadline_exceeded", "offline_lexical_unavailable"]
    assert stopped.is_set()


@pytest.mark.parametrize("mode", ["normal", "symbol_late", "vector_late", "both_late", "search_error"])
@pytest.mark.asyncio
async def test_elapsed_deadline_and_owned_task_completion(monkeypatch, mode):
    from brain.retrieval.service import RetrievalCandidate
    children = []
    service = RetrievalService()

    async def symbols(*args):
        children.append(asyncio.current_task())
        if mode in {"symbol_late", "both_late"}:
            await asyncio.Event().wait()
        await asyncio.sleep(0.001)
        return [RetrievalCandidate("src/core.py", score=1)]

    async def search(*args, **kwargs):
        children.append(asyncio.current_task())
        if mode in {"vector_late", "both_late"}:
            await asyncio.Event().wait()
        if mode == "search_error":
            raise RuntimeError("unavailable")
        await asyncio.sleep(0.001)
        return {"files": [{"path": "src/core.py"}]}

    monkeypatch.setattr(service, "_known_symbol_route", symbols)
    monkeypatch.setattr("brain.retrieval.service.search_code", search)
    started = time.perf_counter()
    result = await asyncio.wait_for(service.retrieve("core", None, deadline_s=0.02), 0.5)
    elapsed = time.perf_counter() - started
    assert elapsed < 0.12, f"request took {elapsed:.3f}s for a 0.020s budget"
    assert all(task.done() for task in children)
    if mode.endswith("late"):
        assert "deadline_exceeded" in result.degraded
    assert bool(result.candidates) == (mode != "both_late")


@pytest.mark.asyncio
async def test_repeated_concurrent_expiry_leaves_no_owned_channel_tasks(monkeypatch):
    children = []

    async def stalled(*args, **kwargs):
        children.append(asyncio.current_task())
        await asyncio.Event().wait()

    monkeypatch.setattr("brain.retrieval.service.search_code", stalled)
    service = RetrievalService()
    monkeypatch.setattr(service, "_known_symbol_route", stalled)
    started = time.perf_counter()
    results = await asyncio.wait_for(asyncio.gather(*(service.retrieve("core", None, deadline_s=0.02) for _ in range(32))), 0.5)
    assert time.perf_counter() - started < 0.25
    assert len(children) == 64 and all(task.done() for task in children)
    assert all(result.degraded == ["deadline_exceeded"] for result in results)
