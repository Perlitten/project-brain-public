"""Backfill pgvector columns and regenerate incompatible embeddings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional, cast as type_cast

from loguru import logger
from sqlalchemy import cast as sa_cast, delete, func, select, text
from sqlalchemy.engine import CursorResult
from sqlalchemy.dialects.postgresql import JSONB

from brain.database.models import Embedding, File, FileChunk
from brain.database.session import async_session_factory
from brain.workers.db_fencing import assert_current_db_fence
from brain.embeddings.config import EmbeddingConfig, get_embedding_config
from brain.embeddings.pgvector_sql import pgvector_cast_type, pgvector_index_dimension
from brain.embeddings.integrity import _embedding_reason
from brain.embeddings.store import build_embedding_record
from brain.llm import get_embedding_provider
from brain.search.filters import should_exclude_from_retrieval
from brain.workers.runtime import check_job_lease


@dataclass
class BackfillResult:
    pgvector_synced: int = 0
    regenerated: int = 0
    failed: int = 0
    skipped: int = 0

    def to_dict(self) -> dict:
        return {
            "pgvector_synced": self.pgvector_synced,
            "regenerated": self.regenerated,
            "failed": self.failed,
            "skipped": self.skipped,
        }


@dataclass
class PendingChunk:
    chunk_id: int
    content: str
    file_path: str
    old_embedding_id: Optional[int]


ADAPTIVE_RETRY_CHARS = (1536, 1024, 768, 512, 256, 128)


def _clean_retry_text(text: str, max_chars: int) -> str:
    clipped = text[:max_chars]
    cleaned = "".join(
        char if char in "\n\r\t" or ord(char) >= 32 else " "
        for char in clipped
    ).strip()
    return cleaned or " "


async def _embed_with_adaptive_retry(provider, item: PendingChunk) -> List[float]:
    """Retry a single problematic chunk with progressively smaller payloads.

    NVIDIA's embed endpoint can return 500 for dense code even after the normal
    provider truncation. Backfill should isolate the bad input and still repair
    the row with a shorter, deterministic prefix instead of leaving coverage
    permanently below 100%.
    """
    last_exc: Exception | None = None
    for max_chars in ADAPTIVE_RETRY_CHARS:
        retry_text = _clean_retry_text(item.content, max_chars)
        try:
            return await provider.embed(retry_text, input_type="passage")
        except Exception as exc:
            last_exc = exc
            logger.warning(
                f"  chunk {item.chunk_id} ({item.file_path}): "
                f"adaptive retry failed at {max_chars} chars: {exc}"
            )
    assert last_exc is not None
    raise last_exc


async def sync_pgvector_from_json(config: Optional[EmbeddingConfig] = None) -> int:
    """Copy JSON vector_data into the pgvector column when dimensions match."""
    cfg = config or get_embedding_config()
    cast_type = pgvector_cast_type(cfg.dimension)
    index_dim = pgvector_index_dimension(cfg.dimension)
    async with async_session_factory() as session:
        async with session.begin():
            await assert_current_db_fence(session)
            result = await session.execute(
                text(
                    f"""
                    UPDATE embeddings
                    SET embedding = (
                        (SELECT jsonb_agg(elem::text::float8 ORDER BY ord)
                         FROM jsonb_array_elements(vector_data::jsonb) WITH ORDINALITY AS t(elem, ord)
                         WHERE ord <= {index_dim})::text
                    )::{cast_type},
                        provider = COALESCE(provider, :provider),
                        model = COALESCE(model, :model),
                        dimension = COALESCE(dimension, :dimension)
                    WHERE embedding IS NULL
                      AND vector_data IS NOT NULL
                      AND jsonb_array_length(vector_data::jsonb) = :dimension
                    """
                ),
                {
                    "provider": cfg.provider,
                    "model": cfg.model,
                    "dimension": cfg.dimension,
                },
            )
            return int(type_cast(CursorResult[Any], result).rowcount or 0)


async def _chunks_needing_regeneration(
    repository_id: Optional[int],
    config: EmbeddingConfig,
) -> List[PendingChunk]:
    pending: List[PendingChunk] = []
    async with async_session_factory() as session:
        stmt = (
            select(
                FileChunk.id,
                FileChunk.content,
                File.path,
                Embedding.id,
                Embedding.dimension,
                Embedding.provider,
                Embedding.model,
                Embedding.content_hash,
                func.jsonb_array_length(sa_cast(Embedding.vector_data, JSONB)).label("vec_len"),
                Embedding.embedding.isnot(None).label("has_vec"),
            )
            .join(File, FileChunk.file_id == File.id)
            .outerjoin(Embedding, FileChunk.embedding_id == Embedding.id)
        )
        if repository_id is not None:
            stmt = stmt.where(File.repository_id == repository_id)
        rows = (await session.execute(stmt)).all()

    for chunk_id, content, path, emb_id, dim, provider, model, chash, vec_len, has_vec in rows:
        if should_exclude_from_retrieval(path):
            continue
        reason = _embedding_reason(emb_id, dim, provider, model, chash, vec_len, content, config)
        if reason == "current" and has_vec:
            continue
        pending.append(
            PendingChunk(
                chunk_id=chunk_id,
                content=content,
                file_path=path,
                old_embedding_id=emb_id,
            )
        )
    return pending


async def _replace_chunk_embedding(
    item: PendingChunk,
    vector: List[float],
    config: EmbeddingConfig,
) -> bool:
    async with async_session_factory() as session:
        try:
            chunk_db = await session.get(FileChunk, item.chunk_id)
            if chunk_db is None:
                return False

            if item.old_embedding_id:
                await session.execute(
                    delete(Embedding).where(Embedding.id == item.old_embedding_id)
                )

            emb_rec = build_embedding_record(
                entity_type="file_chunk",
                entity_id=chunk_db.id,
                vector=vector,
                source_text=chunk_db.content,
                config=config,
            )
            session.add(emb_rec)
            await session.flush()
            chunk_db.embedding_id = emb_rec.id
            emb_rec.entity_id = chunk_db.id
            await check_job_lease()
            await assert_current_db_fence(session)
            await session.commit()
            return True
        except Exception as exc:
            await session.rollback()
            logger.error(f"FAIL chunk {item.chunk_id} ({item.file_path}): {exc}")
            return False


async def regenerate_embeddings(
    repository_id: Optional[int] = None,
    *,
    limit: Optional[int] = None,
    batch_size: int = 8,
    provider_name: Optional[str] = None,
    dry_run: bool = False,
) -> BackfillResult:
    """Regenerate stale/incompatible embeddings and populate pgvector."""
    config = get_embedding_config(provider_name)
    provider = get_embedding_provider(provider_name)
    result = BackfillResult()

    pending = await _chunks_needing_regeneration(repository_id, config)
    if limit is not None:
        pending = pending[:limit]

    if dry_run:
        result.skipped = len(pending)
        return result

    for start in range(0, len(pending), batch_size):
        batch = pending[start : start + batch_size]
        texts = [item.content for item in batch]
        try:
            vectors = await provider.embed_batch(texts, input_type="passage")
            if len(vectors) != len(batch):
                raise ValueError("Embedding repair batch cardinality mismatch")
        except Exception as exc:
            logger.error(f"Embedding batch failed at offset {start}: {exc}")
            for item in batch:
                try:
                    vector = await provider.embed(item.content, input_type="passage")
                except Exception as item_exc:
                    logger.error(
                        f"  chunk {item.chunk_id} ({item.file_path}): "
                        f"single-item retry failed: {item_exc}; trying adaptive truncation"
                    )
                    try:
                        vector = await _embed_with_adaptive_retry(provider, item)
                    except Exception as adaptive_exc:
                        result.failed += 1
                        logger.error(
                            f"  chunk {item.chunk_id} ({item.file_path}): "
                            f"adaptive retry exhausted: {adaptive_exc}"
                        )
                        continue
                if await _replace_chunk_embedding(item, vector, config):
                    result.regenerated += 1
                else:
                    result.failed += 1
            continue

        for item, vector in zip(batch, vectors):
            if await _replace_chunk_embedding(item, vector, config):
                result.regenerated += 1
            else:
                result.failed += 1

    return result


async def backfill_embeddings(
    repository_id: Optional[int] = None,
    *,
    pgvector_only: bool = False,
    limit: Optional[int] = None,
    batch_size: int = 8,
    provider_name: Optional[str] = None,
    dry_run: bool = False,
) -> BackfillResult:
    """Sync JSON vectors to pgvector, then regenerate incompatible rows."""
    config = get_embedding_config(provider_name)
    result = BackfillResult()

    if not dry_run:
        result.pgvector_synced = await sync_pgvector_from_json(config)

    if pgvector_only:
        return result

    regen = await regenerate_embeddings(
        repository_id,
        limit=limit,
        batch_size=batch_size,
        provider_name=provider_name,
        dry_run=dry_run,
    )
    result.regenerated = regen.regenerated
    result.failed = regen.failed
    result.skipped = regen.skipped
    return result
