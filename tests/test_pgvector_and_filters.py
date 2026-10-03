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


def test_failed_pgvector_index_build_keeps_column_swap():
    """A failed ANN build (e.g. /dev/shm exhausted) must stay inside a savepoint
    so the enclosing migration transaction still commits the new column."""
    import asyncio

    from brain.database import migrations

    class Savepoint:
        def __init__(self, conn):
            self.conn = conn

        async def __aenter__(self):
            self.conn.sql.append("SAVEPOINT")

        async def __aexit__(self, exc_type, exc, tb):
            self.conn.sql.append("ROLLBACK TO SAVEPOINT" if exc_type else "RELEASE SAVEPOINT")
            return False

    class Conn:
        def __init__(self):
            self.sql = []

        def begin_nested(self):
            return Savepoint(self)

        async def execute(self, stmt):
            sql = " ".join(str(stmt).split())
            self.sql.append(sql)
            if sql.startswith("CREATE INDEX"):
                raise RuntimeError("could not resize shared memory segment")

    async def current_type(conn):
        return "halfvec(4000)", None

    conn = Conn()
    original = migrations._current_vector_column_type
    migrations._current_vector_column_type = current_type
    try:
        asyncio.run(migrations._ensure_embedding_vector_column(conn, 2048))
    finally:
        migrations._current_vector_column_type = original

    assert "ALTER TABLE embeddings ADD COLUMN embedding halfvec(2048)" in conn.sql
    build = conn.sql.index("SET LOCAL max_parallel_maintenance_workers = 0")
    assert conn.sql[build - 1] == "SAVEPOINT"
    assert conn.sql[build + 1].startswith("CREATE INDEX IF NOT EXISTS idx_embeddings_vector_hnsw_half")
    assert conn.sql[build + 2] == "ROLLBACK TO SAVEPOINT"
