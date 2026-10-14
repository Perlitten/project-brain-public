"""Unusable channel data must not poison successful retrieval results."""
import asyncio
import math
from unittest.mock import AsyncMock

import pytest

from brain.config.settings import settings
from brain.retrieval.service import RetrievalCandidate, RetrievalService


@pytest.mark.parametrize("deadline", [math.nan, math.inf, -math.inf])
@pytest.mark.asyncio
async def test_retrieval_rejects_nonfinite_deadlines_before_starting_io(monkeypatch, deadline):
    search = AsyncMock()
    monkeypatch.setattr("brain.retrieval.service.search_code", search)
    service = RetrievalService()
    symbols = AsyncMock(return_value=[])
    monkeypatch.setattr(service, "_known_symbol_route", symbols)
    with pytest.raises(ValueError, match="deadline"):
        await service.retrieve("core", None, deadline_s=deadline)
    search.assert_not_called()
    symbols.assert_not_called()


@pytest.mark.parametrize("score", [math.nan, math.inf, -math.inf, "broken"])
@pytest.mark.parametrize("channel", ["files", "chunks"])
@pytest.mark.parametrize("v2", [False, True])
@pytest.mark.asyncio
async def test_invalid_scores_preserve_other_rows_and_report_degradation(monkeypatch, score, channel, v2):
    monkeypatch.setattr(settings, "RETRIEVAL_V2_ENABLED", v2)
    raw = {"files": [{"path": "src/good.py"}]}
    if channel == "files":
        raw["files"].append({"path": "src/bad.py", "lexical_score": score})
    else:
        raw["chunks"] = [{"file_path": "src/bad.py", "similarity": score}]
    monkeypatch.setattr("brain.retrieval.service.search_code", AsyncMock(return_value=raw))
    service = RetrievalService()
    monkeypatch.setattr(service, "_known_symbol_route", AsyncMock(return_value=[]))
    result = await service.retrieve("good", None)
    assert [candidate.path for candidate in result.candidates] == ["src/good.py"]
    assert "invalid_retrieval_scores" in result.degraded
    assert all(math.isfinite(candidate.score) for candidate in result.candidates)


def test_null_lexical_score_uses_rank_fallback():
    candidates = RetrievalService._normalize({"files": [{"path": "src/good.py", "lexical_score": None}]}, 5)
    assert [candidate.path for candidate in candidates] == ["src/good.py"]
    assert math.isfinite(candidates[0].score)


def test_boolean_line_numbers_are_not_source_ranges():
    candidate = RetrievalCandidate("src/good.py")
    candidate.add_range(True, 2)
    candidate.add_range(1, True)
    assert candidate.ranges == []
    candidate.add_range(1, 2)
    assert candidate.ranges == [(1, 2)]


@pytest.mark.asyncio
async def test_cancelling_one_request_does_not_cancel_parallel_request(monkeypatch):
    entered = asyncio.Event()
    stopped = asyncio.Event()
    release = asyncio.Event()

    async def search(query, **kwargs):
        if query == "cancel":
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        await release.wait()
        return {"files": [{"path": "src/healthy.py"}]}

    service = RetrievalService()
    monkeypatch.setattr(service, "_known_symbol_route", AsyncMock(return_value=[]))
    monkeypatch.setattr("brain.retrieval.service.search_code", search)
    cancelled = asyncio.create_task(service.retrieve("cancel", None))
    healthy = asyncio.create_task(service.retrieve("healthy", None))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        assert stopped.is_set()
        assert not healthy.done()
        release.set()
        result = await asyncio.wait_for(healthy, 1)
        assert [candidate.path for candidate in result.candidates] == ["src/healthy.py"]
        assert result.degraded == []
    finally:
        release.set()
        for task in (cancelled, healthy):
            if not task.done():
                task.cancel()
        await asyncio.gather(cancelled, healthy, return_exceptions=True)
