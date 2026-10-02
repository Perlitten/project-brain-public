"""Vector search must not silently fall back without explicit dev flag."""

from types import SimpleNamespace
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from brain.embeddings.constants import VectorSearchStatus
from brain.search.code_search import _pgvector_chunk_search, vector_search_chunks
from brain.search.filters import annotate_knowledge_summary, KNOWLEDGE_STATUS_HISTORICAL


@pytest.mark.asyncio
async def test_vector_search_degraded_without_pgvector_matches(monkeypatch):
    from brain.config.settings import settings

    monkeypatch.setattr(settings, "ALLOW_EMBEDDING_JSON_FALLBACK", False)

    with patch("brain.search.code_search._embed_query", AsyncMock(return_value=[0.1] * 1536)):
        with patch("brain.search.code_search._pgvector_chunk_search", AsyncMock(return_value=[])):
            result = await vector_search_chunks("health check endpoint", top_k=5)
    assert result.status in {VectorSearchStatus.DEGRADED, VectorSearchStatus.OK}
    assert result.matches == []


@pytest.mark.asyncio
async def test_pgvector_candidate_pool_is_reranked_by_knowledge_authority():
    historical_chunk = SimpleNamespace(id=1, file_id=11)
    current_chunk = SimpleNamespace(id=2, file_id=12)
    historical_file = SimpleNamespace(
        id=11,
        path="docs/old-plan.md",
        file_type="docs",
        summary=annotate_knowledge_summary(
            "Superseded plan",
            KNOWLEDGE_STATUS_HISTORICAL,
        ),
    )
    current_file = SimpleNamespace(
        id=12,
        path="docs/current-contract.md",
        file_type="docs",
        summary="Current contract",
    )

    row_result = MagicMock()
    row_result.mappings.return_value.all.return_value = [
        {"chunk_id": 1, "similarity": 0.99, "file_id": 11},
        {"chunk_id": 2, "similarity": 0.80, "file_id": 12},
    ]
    chunk_result = MagicMock()
    chunk_result.scalars.return_value.all.return_value = [
        historical_chunk,
        current_chunk,
    ]
    file_result = MagicMock()
    file_result.scalars.return_value.all.return_value = [
        historical_file,
        current_file,
    ]
    session = AsyncMock()
    session.execute.side_effect = [row_result, chunk_result, file_result]

    with patch(
        "brain.search.code_search.get_embedding_config",
        return_value=SimpleNamespace(dimension=4096),
    ), patch(
        "brain.search.code_search._format_pgvector",
        return_value="[0.1]",
    ):
        matches = await _pgvector_chunk_search(
            session,
            "current production contract",
            [0.1],
            top_k=1,
            repository_id=None,
        )

    assert len(matches) == 1
    assert matches[0][1] is current_chunk
