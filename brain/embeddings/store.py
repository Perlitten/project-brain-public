"""Create and validate embedding rows with full metadata."""

from __future__ import annotations

from typing import List, Optional

from sqlalchemy import text

from brain.database.models import Embedding
from brain.embeddings.config import EmbeddingConfig, content_hash, get_embedding_config
from brain.embeddings.constants import VectorSearchStatus
from brain.embeddings.pgvector_sql import (
    format_pgvector_literal,
    pgvector_cast_type,
    truncate_vector_for_index,
)


class EmbeddingDimensionError(ValueError):
    """Raised when a vector length does not match configured dimension."""


def validate_vector(vector: List[float], config: Optional[EmbeddingConfig] = None) -> None:
    cfg = config or get_embedding_config()
    if not vector:
        raise EmbeddingDimensionError("Empty embedding vector")
    if len(vector) != cfg.dimension:
        raise EmbeddingDimensionError(
            f"Vector dimension {len(vector)} != configured {cfg.dimension}"
        )


def build_embedding_record(
    entity_type: str,
    entity_id: int,
    vector: List[float],
    source_text: str,
    config: Optional[EmbeddingConfig] = None,
) -> Embedding:
    cfg = config or get_embedding_config()
    validate_vector(vector, cfg)
    indexed = truncate_vector_for_index(vector, cfg.dimension)
    return Embedding(
        entity_type=entity_type,
        entity_id=entity_id,
        vector_data=vector,
        embedding=indexed,
        provider=cfg.provider,
        model=cfg.model,
        dimension=cfg.dimension,
        content_hash=content_hash(source_text),
    )


async def populate_pgvector_embedding(session, embedding_id: int, vector: List[float], config: Optional[EmbeddingConfig] = None) -> None:
    """Write the ANN-indexed pgvector column using the correct SQL cast."""
    cfg = config or get_embedding_config()
    indexed = truncate_vector_for_index(vector, cfg.dimension)
    cast_type = pgvector_cast_type(cfg.dimension)
    literal = format_pgvector_literal(indexed)
    await session.execute(
        text(
            f"""
            UPDATE embeddings
            SET embedding = CAST(:vector_literal AS {cast_type}),
                dimension = COALESCE(dimension, :dimension)
            WHERE id = :embedding_id
            """
        ),
        {
            "vector_literal": literal,
            "dimension": cfg.dimension,
            "embedding_id": embedding_id,
        },
    )


def vector_search_status_from_error(exc: Exception) -> str:
    if isinstance(exc, EmbeddingDimensionError):
        return VectorSearchStatus.DIMENSION_MISMATCH
    msg = str(exc).lower()
    if "vector" in msg or "pgvector" in msg:
        return VectorSearchStatus.PGVECTOR_UNAVAILABLE
    return VectorSearchStatus.DEGRADED
