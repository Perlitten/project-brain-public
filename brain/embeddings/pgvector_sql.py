"""pgvector column types, dimension limits, and SQL helpers."""

from __future__ import annotations

from typing import List

PGVECTOR_VECTOR_MAX_DIM = 2000
PGVECTOR_INDEX_MAX_DIM = 4000


def pgvector_index_dimension(configured_dimension: int) -> int:
    """Dimensions stored in the ANN-indexed pgvector column."""
    return min(configured_dimension, PGVECTOR_INDEX_MAX_DIM)


def pgvector_column_sql_type(configured_dimension: int) -> str:
    """SQL type for the embeddings.embedding column."""
    if configured_dimension <= PGVECTOR_VECTOR_MAX_DIM:
        return f"vector({configured_dimension})"
    index_dim = pgvector_index_dimension(configured_dimension)
    return f"halfvec({index_dim})"


def pgvector_distance_ops(configured_dimension: int) -> str:
    """pgvector operator class for cosine distance indexes/queries."""
    if configured_dimension <= PGVECTOR_VECTOR_MAX_DIM:
        return "vector_cosine_ops"
    return "halfvec_cosine_ops"


def pgvector_cast_type(configured_dimension: int) -> str:
    """CAST target for query vectors in SQL."""
    return pgvector_column_sql_type(configured_dimension)


def truncate_vector_for_index(vector: List[float], configured_dimension: int) -> List[float]:
    """Truncate a full embedding to the indexed pgvector width."""
    index_dim = pgvector_index_dimension(configured_dimension)
    if len(vector) < index_dim:
        raise ValueError(f"Vector length {len(vector)} < indexed dimension {index_dim}")
    return vector[:index_dim]


def format_pgvector_literal(vector: List[float]) -> str:
    return "[" + ",".join(str(float(v)) for v in vector) + "]"


def pgvector_index_names() -> tuple[str, ...]:
    return (
        "idx_embeddings_vector_cosine",
        "idx_embeddings_vector_hnsw",
        "idx_embeddings_vector_hnsw_half",
    )
