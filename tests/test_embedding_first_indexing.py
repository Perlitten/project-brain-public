"""Real SQL projection with foreground chat access forbidden."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select

from brain.config.settings import settings
from brain.database.models import Embedding, File, FileChunk, Symbol
from brain.indexers.file_indexer import FileIndexer
from brain.indexers.source_summaries import source_summaries
from tests.test_indexing_reliability import indexing_db as indexing_db

SOURCE = '''"""Payments module."""
def charge(amount: int) -> int:
    """Validate a payment."""
    return amount
'''


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["lazy", "off"])
async def test_foreground_is_complete_without_chat(indexing_db, tmp_path, monkeypatch, mode):
    sessions, graph, verification = indexing_db
    monkeypatch.delenv("FAST_INDEX", raising=False)
    monkeypatch.setenv("BRAIN_CHUNK_SUMMARIES", "1")
    monkeypatch.setattr(settings, "INDEX_LLM_SUMMARIES_MODE", mode)
    monkeypatch.setattr(settings, "INDEX_LLM_SUMMARIES_BACKGROUND", False)
    source = tmp_path / "payments.py"
    source.write_text(SOURCE)
    indexer = FileIndexer()
    forbidden = Mock(side_effect=AssertionError("Foreground indexing accessed chat"))
    monkeypatch.setattr(indexer.router, "summarizer", forbidden)
    result = await indexer.index_repository(tmp_path)
    assert result.indexing_status == "completed"
    assert indexer.verification["source_revision_verified"]
    forbidden.assert_not_called()
    verification.assert_awaited_once()
    async with sessions() as session:
        file = (await session.scalars(select(File).where(File.repository_id == result.id))).one()
        assert "Payments module" in file.summary and "charge" in file.summary
        symbols = (await session.scalars(select(Symbol).where(Symbol.file_id == file.id))).all()
        assert any("Validate a payment" in symbol.summary for symbol in symbols)
        chunks = (await session.scalars(select(FileChunk).where(FileChunk.file_id == file.id))).all()
        assert chunks and all(chunk.embedding_id for chunk in chunks)
        embeddings = (await session.scalars(select(Embedding).where(
            Embedding.id.in_([chunk.embedding_id for chunk in chunks])))).all()
        assert len(embeddings) == len(chunks) and all(embedding.vector_data for embedding in embeddings)
    assert graph.create_node.await_count >= 3
    assert graph.create_relationship.await_count + graph.create_relationships.await_count > 0
    await indexer.index_repository(tmp_path)
    assert indexer.progress["files"]["skipped"] == 1
    forbidden.assert_not_called()


@pytest.mark.asyncio
async def test_inline_preserves_opt_in_provider_calls(monkeypatch):
    monkeypatch.delenv("FAST_INDEX", raising=False)
    monkeypatch.setenv("BRAIN_CHUNK_SUMMARIES", "1")
    monkeypatch.setattr(settings, "INDEX_LLM_SUMMARIES_MODE", "inline")
    summarizer = SimpleNamespace(summarize=AsyncMock(return_value="Generated summary"))
    indexer = FileIndexer()
    monkeypatch.setattr(indexer.router, "summarizer", lambda: summarizer)
    prepared = await indexer._prepare_file(SOURCE, ".py", "payments.py", "source_code", "current")
    assert prepared.summary == "Generated summary"
    assert summarizer.summarize.await_count == 1 + len(prepared.chunks)


@pytest.mark.asyncio
async def test_enrichment_cannot_overwrite_changed_source(indexing_db, tmp_path, monkeypatch):
    from brain.indexers import summary_enrichment

    sessions, _, _ = indexing_db
    monkeypatch.setattr(summary_enrichment, "async_session_factory", sessions)
    source = tmp_path / "payments.py"
    source.write_text(SOURCE)
    indexer = FileIndexer()
    result = await indexer.index_repository(tmp_path)

    async def synthesize(content):
        source.write_text(SOURCE + "# changed during synthesis\n")
        return "Obsolete prose"

    monkeypatch.setattr(summary_enrichment, "get_model_router", lambda: SimpleNamespace(
        summarizer=lambda: SimpleNamespace(summarize=AsyncMock(side_effect=synthesize))))
    counts = await summary_enrichment.enrich_summaries(result.id, tmp_path)
    assert counts == {"updated": 0, "skipped": 1, "failed": 0}
    async with sessions() as session:
        file = (await session.scalars(select(File).where(File.repository_id == result.id))).one()
        assert file.summary != "Obsolete prose"


@pytest.mark.asyncio
async def test_optional_queue_failure_cannot_fail_verified_index(indexing_db, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "INDEX_LLM_SUMMARIES_MODE", "lazy")
    monkeypatch.setattr(settings, "INDEX_LLM_SUMMARIES_BACKGROUND", True)
    queue = SimpleNamespace(enqueue=AsyncMock(side_effect=ConnectionError("offline")))
    monkeypatch.setattr("brain.workers.queue.queue_for_job", lambda *args, **kwargs: queue)
    (tmp_path / "payments.py").write_text(SOURCE)
    result = await FileIndexer().index_repository(tmp_path)
    assert result.indexing_status == "completed"
    queue.enqueue.assert_awaited_once()


def test_deterministic_summaries_are_bounded_on_malformed_source():
    result = source_summaries("# Header\ninvalid (", ".py", "bad.py", [])
    assert "Header" in result.file
    result = source_summaries('"""' + "x" * 10000 + '"""', ".py", "large.py", [])
    assert len(result.file) <= 3200
