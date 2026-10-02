import pytest

from brain.retrieval.service import RetrievalService


@pytest.mark.asyncio
async def test_known_symbol_route_is_not_dependent_on_vector_search(monkeypatch):
    service = RetrievalService()

    async def known_symbol(*_args, **_kwargs):
        from brain.retrieval.service import RetrievalCandidate
        return [RetrievalCandidate(path="brain/search/code_search.py", symbols=["search_code"], score=1.0)]

    monkeypatch.setattr(service, "_known_symbol_route", known_symbol)
    result = await service.retrieve("search_code", None, deadline_s=0.001)
    assert result.repository["fast_route"] == "symbol"
    assert result.candidates[0].path == "brain/search/code_search.py"
