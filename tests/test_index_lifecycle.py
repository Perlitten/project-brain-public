"""Committed Git mutations must retire every obsolete source projection."""

import subprocess
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from brain.database.models import Embedding, File, FileCard, FileChunk
from brain.indexers.file_indexer import FileIndexer
from tests.test_indexing_reliability import indexing_db as indexing_db


def _commit(root):
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "commit", "-qm", "fixture"], check=True, capture_output=True,
    )


def _source(root):
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True, capture_output=True)
    source = root / "payments.py"
    source.write_text("def retired_payment_marker():\n    return 1\n")
    _commit(root)
    return source


@pytest.mark.asyncio
@pytest.mark.parametrize("exclusion", ["binary", "large"])
async def test_newly_excluded_source_retires_previous_projection(indexing_db, tmp_path, exclusion, monkeypatch):
    sessions, graph, _ = indexing_db
    source = _source(tmp_path)
    repo = await FileIndexer().index_repository(tmp_path)
    async with sessions() as session:
        old_chunk = (await session.scalars(select(FileChunk).join(File).where(
            File.repository_id == repo.id))).one()
        embedding_id = old_chunk.embedding_id

    if exclusion == "binary":
        source.write_bytes(b"\x00binary payload\n")
    else:
        source.write_bytes(b"x" * (10 * 1024 * 1024 + 1))
    _commit(tmp_path)
    replay = FileIndexer()
    graph_enrichment = AsyncMock()
    monkeypatch.setattr(replay, "_post_process_graph", graph_enrichment)
    result = await replay.index_repository(tmp_path)
    assert result.indexing_status == "completed"
    assert replay.verification["source_revision_verified"]
    async with sessions() as session:
        assert not (await session.scalars(select(File).where(File.repository_id == repo.id))).all()
        assert await session.get(FileChunk, old_chunk.id) is None
        assert await session.get(Embedding, embedding_id) is None
    graph.delete_repository_files_not_in.assert_awaited_with(set())
    assert graph_enrichment.await_args.args[2] == []


@pytest.mark.asyncio
async def test_transient_index_failure_preserves_previous_projection(indexing_db, tmp_path, monkeypatch):
    sessions, graph, _ = indexing_db
    source = _source(tmp_path)
    repo = await FileIndexer().index_repository(tmp_path)
    async with sessions() as session:
        chunk = (await session.scalars(select(FileChunk).join(File).where(
            File.repository_id == repo.id))).one()
        embedding_id = chunk.embedding_id
    source.write_text("def current_payment_marker():\n    return 3\n")
    _commit(tmp_path)
    replay = FileIndexer()
    monkeypatch.setattr(replay, "index_file", AsyncMock(side_effect=OSError("transient read failure")))
    result = await replay.index_repository(tmp_path)
    assert result.indexing_status == "failed"
    assert replay.progress["files"]["failed"] == 1
    async with sessions() as session:
        assert await session.get(FileChunk, chunk.id) is not None
        assert await session.get(Embedding, embedding_id) is not None
    graph.delete_repository_files_not_in.assert_awaited_with({"payments.py"})


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["modify", "rename", "delete"])
async def test_committed_mutations_retire_chunk_and_card_embeddings(indexing_db, tmp_path, mutation):
    sessions, _, _ = indexing_db
    source = _source(tmp_path)
    (tmp_path / "unchanged.py").write_text("def stable_marker():\n    return 2\n")
    _commit(tmp_path)
    repo = await FileIndexer().index_repository(tmp_path)
    async with sessions.begin() as session:
        files = (await session.scalars(select(File).where(File.repository_id == repo.id))).all()
        target = next(file for file in files if file.path == "payments.py")
        stable = next(file for file in files if file.path == "unchanged.py")
        chunk = (await session.scalars(select(FileChunk).where(FileChunk.file_id == target.id))).one()
        stable_chunk = (await session.scalars(select(FileChunk).where(FileChunk.file_id == stable.id))).one()
        old_embedding_id = chunk.embedding_id
        stable_embedding_id = stable_chunk.embedding_id
        card = FileCard(repository_id=repo.id, file_id=target.id, path=target.path,
                        card_text="retired card", card_hash="fixture")
        session.add(card)
        await session.flush()
        card_embedding = Embedding(entity_type="file_card", entity_id=card.id, vector_data=[1.0])
        unrelated = Embedding(entity_type="decision", entity_id=chunk.id, vector_data=[1.0])
        session.add_all([card_embedding, unrelated])
        await session.flush()
        card.embedding_id = card_embedding.id
        card_embedding_id, unrelated_id, card_id = card_embedding.id, unrelated.id, card.id

    if mutation == "modify":
        source.write_text("def current_payment_marker():\n    return 3\n")
    elif mutation == "rename":
        source.rename(tmp_path / "renamed.py")
    else:
        source.unlink()
    _commit(tmp_path)
    result = await FileIndexer().index_repository(tmp_path)
    assert result.indexing_status == "completed"
    async with sessions() as session:
        assert await session.get(Embedding, old_embedding_id) is None
        assert await session.get(Embedding, card_embedding_id) is None
        assert await session.get(FileCard, card_id) is None
        assert await session.get(Embedding, stable_embedding_id) is not None
        assert await session.get(Embedding, unrelated_id) is not None
        paths = (await session.scalars(select(File.path).where(File.repository_id == repo.id))).all()
        expected = {"unchanged.py"} | ({"payments.py"} if mutation == "modify" else
                                     {"renamed.py"} if mutation == "rename" else set())
        assert set(paths) == expected
        assert len(paths) == len(expected)
    # The unrelated embedding deliberately outlives source rows; do not leak it
    # into other tests that share the PostgreSQL fixture database.
    async with sessions.begin() as session:
        await session.delete(await session.get(Embedding, unrelated_id))
