"""Tests for secret/env exclusion and pgvector SQL helpers."""

from brain.embeddings.pgvector_sql import (
    pgvector_cast_type,
    pgvector_column_sql_type,
    pgvector_index_dimension,
    truncate_vector_for_index,
)
from brain.search.filters import is_secret_or_env_file, should_exclude_from_indexing, should_exclude_from_retrieval


def test_env_files_excluded_from_index_and_retrieval():
    assert is_secret_or_env_file(".env")
    assert is_secret_or_env_file(".env.local")
    assert should_exclude_from_indexing(".env")
    assert should_exclude_from_indexing("config/.env.production")
    assert should_exclude_from_retrieval("reports/secret.env")


def test_pgvector_halfvec_for_nvidia_dimension():
    assert pgvector_column_sql_type(4096) == "halfvec(4000)"
    assert pgvector_index_dimension(4096) == 4000
    assert pgvector_cast_type(1536) == "vector(1536)"


def test_truncate_vector_for_index():
    full = [float(i) for i in range(4096)]
    truncated = truncate_vector_for_index(full, 4096)
    assert len(truncated) == 4000
    assert truncated[0] == 0.0
    assert truncated[-1] == 3999.0
