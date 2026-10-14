"""Offline output and failure-state contracts for retrieval adapters."""
import asyncio
import gc
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from brain.config.settings import settings
from brain.retrieval.service import RetrievalCandidate, RetrievalService


def test_source_ranges_include_single_lines_but_never_line_zero():
    candidate = RetrievalCandidate("src/code.py")
    for start, end in [(0, 1), (-1, 2), (3, 2), (1, None), ("1", 2)]:
        candidate.add_range(start, end)
    assert candidate.ranges == []
    candidate.add_range(1, 1)
    candidate.add_range(1, 1)
    assert candidate.ranges == [(1, 1)]


@pytest.mark.parametrize("channel,path_key", [("files", "path"), ("symbols", "path"), ("chunks", "file_path")])
@pytest.mark.parametrize("v2", [False, True])
def test_normalization_skips_incomplete_rows_and_ranks_valid_channels(monkeypatch, channel, path_key, v2):
    monkeypatch.setattr(settings, "RETRIEVAL_V2_ENABLED", v2)
    raw = {channel: [None, {}, {path_key: "src/first.py"}, {path_key: "src/second.py"}]}
    rows = RetrievalService._normalize(raw, 2)
    assert [row.path for row in rows] == ["src/first.py", "src/second.py"]
    score_key = {"files": "lexical", "symbols": "symbol", "chunks": "vector"}[channel]
    assert rows[0].channel_scores[score_key] > rows[1].channel_scores[score_key] > 0
    assert rows[0].score > rows[1].score > 0
    assert len(RetrievalService._normalize(raw, 1)) == 1


@pytest.mark.asyncio
async def test_missing_repository_never_starts_unscoped_channels(monkeypatch):
    monkeypatch.setattr("brain.retrieval.service.get_repository_by_path", AsyncMock(return_value=None))
    search = AsyncMock()
    monkeypatch.setattr("brain.retrieval.service.search_code", search)
    service = RetrievalService()
    symbols = AsyncMock()
    monkeypatch.setattr(service, "_known_symbol_route", symbols)
    result = await service.retrieve("find", "/missing")
    assert result.candidates == []
    assert result.degraded == ["repository_not_indexed"]
    symbols.assert_not_called()
    search.assert_not_called()


@pytest.mark.asyncio
async def test_zero_deadline_never_starts_work(monkeypatch):
    search = AsyncMock()
    lookup = AsyncMock()
    monkeypatch.setattr("brain.retrieval.service.search_code", search)
    monkeypatch.setattr("brain.retrieval.service.get_repository_by_path", lookup)
    monkeypatch.setattr("brain.retrieval.service.time.perf_counter", lambda: 100.0)
    result = await RetrievalService().retrieve("find", "/repo", deadline_s=0)
    assert result.candidates == []
    assert result.degraded == ["deadline_exceeded"]
    search.assert_not_called()
    lookup.assert_not_called()


@pytest.mark.asyncio
async def test_scope_lookup_using_last_deadline_budget_cannot_start_channels(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("brain.retrieval.service.time.perf_counter", lambda: clock[0])

    async def lookup(*args):
        clock[0] = 100.05
        return SimpleNamespace(id=7)

    search, symbols = AsyncMock(), AsyncMock()
    monkeypatch.setattr("brain.retrieval.service.get_repository_by_path", lookup)
    monkeypatch.setattr("brain.retrieval.service.search_code", search)
    service = RetrievalService()
    monkeypatch.setattr(service, "_known_symbol_route", symbols)
    result = await service.retrieve("find", "/repo", deadline_s=0.05)
    assert result.degraded == ["deadline_exceeded"]
    assert result.candidates == []
    search.assert_not_called()
    symbols.assert_not_called()


@pytest.mark.asyncio
async def test_normalization_using_last_deadline_budget_preserves_results_and_reports_expiry(monkeypatch):
    monkeypatch.setattr(settings, "DEFAULT_EMBEDDING_PROVIDER", "mock")
    clock = [100.0]
    monkeypatch.setattr("brain.retrieval.service.time.perf_counter", lambda: clock[0])
    service = RetrievalService()
    original = service._normalize

    def normalize(*args):
        rows = original(*args)
        clock[0] = 100.05
        return rows

    monkeypatch.setattr(service, "_normalize", normalize)
    monkeypatch.setattr(service, "_known_symbol_route", AsyncMock(return_value=[]))
    monkeypatch.setattr("brain.retrieval.service.search_code", AsyncMock(return_value={"files": [{"path": "src/good.py"}]}))
    result = await service.retrieve("find", None, deadline_s=0.05)
    assert [row.path for row in result.candidates] == ["src/good.py"]
    assert result.degraded == ["deadline_exceeded"]


@pytest.mark.asyncio
async def test_large_requested_budget_keeps_primary_channel_bounded(monkeypatch):
    search, symbols = AsyncMock(return_value={}), AsyncMock(return_value=[])
    monkeypatch.setattr("brain.retrieval.service.search_code", search)
    service = RetrievalService()
    monkeypatch.setattr(service, "_known_symbol_route", symbols)
    await service.retrieve("find", None, candidate_budget=25)
    search.assert_awaited_once_with("find", limit=10, repo_path=None)
    symbols.assert_awaited_once_with("find", None, 25)


@pytest.mark.asyncio
async def test_symbol_failure_is_reported_without_hiding_primary_success(monkeypatch):
    service = RetrievalService()
    monkeypatch.setattr(service, "_known_symbol_route", AsyncMock(side_effect=RuntimeError("offline")))
    monkeypatch.setattr("brain.retrieval.service.search_code", AsyncMock(return_value={"files": [{"path": "src/good.py"}]}))
    result = await service.retrieve("find", None)
    assert [row.path for row in result.candidates] == ["src/good.py"]
    assert result.degraded == ["symbol_error:RuntimeError"]


@pytest.mark.asyncio
async def test_fallback_reports_freshness_failure_and_preserves_repository_identity(monkeypatch):
    service = RetrievalService()
    repo = SimpleNamespace(id=7, path="/repo", last_indexed_commit="revision")
    monkeypatch.setattr("brain.retrieval.service.get_repository_by_path", AsyncMock(return_value=repo))
    monkeypatch.setattr(service, "_known_symbol_route", AsyncMock(return_value=[RetrievalCandidate("src/symbol.py")]))
    monkeypatch.setattr("brain.retrieval.service.search_code", AsyncMock(side_effect=RuntimeError("offline")))
    monkeypatch.setattr("brain.retrieval.service.assess_repository_freshness", AsyncMock(side_effect=ValueError("offline")))
    result = await service.retrieve("find", "/repo")
    assert [row.path for row in result.candidates] == ["src/symbol.py"]
    assert result.repository["repository_id"] == 7
    assert result.repository["indexed_revision"] == "revision"
    assert result.repository["freshness"] == {"status": "unknown"}
    assert result.degraded == ["search_error:RuntimeError", "freshness_error:ValueError"]


@pytest.mark.parametrize("provider", ["mock", "local"])
@pytest.mark.asyncio
async def test_bm25_is_local_only_and_successful_rows_are_admitted(monkeypatch, provider):
    monkeypatch.setattr(settings, "RETRIEVAL_V2_ENABLED", True)
    monkeypatch.setattr(settings, "DEFAULT_EMBEDDING_PROVIDER", provider)
    monkeypatch.setattr("brain.retrieval.service.get_repository_by_path", AsyncMock(return_value=SimpleNamespace(id=7)))
    monkeypatch.setattr("brain.retrieval.service.search_code", AsyncMock(return_value={"files": [{"path": "src/primary.py"}]}))
    lexical = AsyncMock(return_value=[{"path": "src/lexical.py", "score": 2.0, "start": 1, "end": 2}])
    monkeypatch.setattr("brain.search.local_lexical.local_lexical", lexical)
    service = RetrievalService()
    monkeypatch.setattr(service, "_known_symbol_route", AsyncMock(return_value=[]))
    result = await service.retrieve("find", "/repo")
    if provider == "local":
        lexical.assert_awaited_once_with(7, "find", 10)
        assert {row.path for row in result.candidates} == {"src/primary.py", "src/lexical.py"}
        assert next(row for row in result.candidates if row.path == "src/lexical.py").ranges == [(1, 2)]
    else:
        lexical.assert_not_called()
        assert [row.path for row in result.candidates] == ["src/primary.py"]
    assert result.degraded == []


@pytest.mark.asyncio
async def test_timings_measure_elapsed_work_not_absolute_clock(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("brain.retrieval.service.time.perf_counter", lambda: clock[0])

    async def search(*args, **kwargs):
        clock[0] += 0.02
        return {"files": [{"path": "src/good.py"}]}

    monkeypatch.setattr("brain.retrieval.service.search_code", search)
    service = RetrievalService()
    monkeypatch.setattr(service, "_known_symbol_route", AsyncMock(return_value=[]))
    result = await service.retrieve("find", None)
    assert result.timings_ms["total"] == pytest.approx(20.0)
    assert result.degraded == []


@pytest.mark.asyncio
async def test_late_provider_cleanup_failure_is_reaped(monkeypatch):
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    errors = []
    release, cleaned = asyncio.Event(), asyncio.Event()
    loop.set_exception_handler(lambda _loop, context: errors.append(context))

    async def search(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            await release.wait()
            cleaned.set()
            raise RuntimeError("late provider cleanup")

    service = RetrievalService()
    monkeypatch.setattr(service, "_known_symbol_route", AsyncMock(return_value=[]))
    monkeypatch.setattr("brain.retrieval.service.search_code", search)
    try:
        result = await asyncio.wait_for(service.retrieve("find", None, deadline_s=0.01), 0.25)
        assert result.degraded == ["deadline_exceeded"]
        release.set()
        await asyncio.wait_for(cleaned.wait(), 0.25)
        for _ in range(3):
            await asyncio.sleep(0)
            gc.collect()
        assert errors == []
    finally:
        release.set()
        loop.set_exception_handler(previous)
