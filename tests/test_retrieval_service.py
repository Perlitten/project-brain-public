import pytest

from brain.retrieval.service import RetrievalService
from brain.embeddings.constants import VectorSearchStatus


def test_retrieval_service_normalizes_files_symbols_and_chunk_ranges():
    candidates = RetrievalService._normalize(
        {
            "files": [{"path": "apps/api/routers/core.py"}],
            "symbols": [{"path": "apps/api/routers/core.py", "name": "search_code_endpoint", "start_line": 1, "end_line": 3}],
            "chunks": [{"file_path": "apps/api/routers/core.py", "start_line": 10, "end_line": 20, "similarity": 0.9}],
        },
        5,
    )
    assert [candidate.path for candidate in candidates] == ["apps/api/routers/core.py"]
    assert candidates[0].symbols == ["search_code_endpoint"]
    assert (10, 20) in candidates[0].ranges
    assert set(candidates[0].channel_scores) == {"lexical", "symbol", "vector"}


@pytest.mark.asyncio
async def test_retrieval_service_returns_typed_deadline_degradation(monkeypatch):
    service = RetrievalService()

    async def no_symbol(*_args, **_kwargs):
        return []

    async def slow_search(*_args, **_kwargs):
        import asyncio
        await asyncio.sleep(0.05)
        return {}

    monkeypatch.setattr(service, "_known_symbol_route", no_symbol)
    monkeypatch.setattr("brain.retrieval.service.search_code", slow_search)
    result = await service.retrieve("unknown location", None, deadline_s=0.001)
    assert result.candidates == []
    assert result.degraded == ["deadline_exceeded"]


@pytest.mark.parametrize("status", [None, VectorSearchStatus.OK, "ok", " OK ",
                                    VectorSearchStatus.DEGRADED,
                                    VectorSearchStatus.DIMENSION_MISMATCH,
                                    VectorSearchStatus.PGVECTOR_UNAVAILABLE])
@pytest.mark.asyncio
async def test_vector_health_does_not_report_success_as_missing(monkeypatch, status):
    service = RetrievalService()

    async def no_symbol(*_args, **_kwargs):
        return []

    async def search(*_args, **_kwargs):
        return {"vector_status": status, "files": [], "symbols": [], "chunks": []}

    monkeypatch.setattr(service, "_known_symbol_route", no_symbol)
    monkeypatch.setattr("brain.retrieval.service.search_code", search)
    result = await service.retrieve("unknown location", None)
    expected = [] if status is None or str(status).strip().casefold() == "ok" else [f"vector:{status}"]
    assert result.degraded == expected
