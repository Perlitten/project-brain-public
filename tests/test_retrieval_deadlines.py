import asyncio

import pytest

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
    result = await service.retrieve("search_code", None, deadline_s=0.001)
    assert result.repository["fast_route"] == "symbol"
    assert result.candidates[0].path == "brain/search/code_search.py"
    assert result.degraded == ["deadline_exceeded"]
    assert vector_cancelled.is_set()
