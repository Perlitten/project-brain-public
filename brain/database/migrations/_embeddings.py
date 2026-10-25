"""Embedding and late-interaction schema: pgvector columns, indexes, LI tables."""
from __future__ import annotations

from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection
from brain.embeddings.constants import EMBEDDING_DIMENSION
from brain.embeddings.pgvector_sql import (PGVECTOR_VECTOR_MAX_DIM, pgvector_column_sql_type, pgvector_distance_ops, pgvector_index_dimension, pgvector_index_names)


async def _ensure_embedding_column(conn: AsyncConnection, table: str) -> None:
    """Add the pgvector ``embedding`` column to a memory table (memory_learnings,
    memory_episodes, memory_skills all repeat this block)."""
    vec_type = pgvector_column_sql_type(EMBEDDING_DIMENSION)
    await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS embedding {vec_type}"))


async def _ensure_embedding_metadata_columns(conn: AsyncConnection) -> None:
    for ddl in (
        "ALTER TABLE embeddings ADD COLUMN IF NOT EXISTS provider VARCHAR(64)",
        "ALTER TABLE embeddings ADD COLUMN IF NOT EXISTS model VARCHAR(128)",
        "ALTER TABLE embeddings ADD COLUMN IF NOT EXISTS dimension INTEGER",
        "ALTER TABLE embeddings ADD COLUMN IF NOT EXISTS content_hash VARCHAR(64)",
    ):
        try:
            await conn.execute(text(ddl))
        except Exception as exc:
            logger.warning(f"Could not apply metadata column migration: {exc}")


async def _ensure_late_interaction_table(conn: AsyncConnection) -> None:
    """Create the additive ColBERT store without changing dense embeddings."""
    await conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS late_interaction_embeddings (
                id SERIAL PRIMARY KEY,
                repository_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
                chunk_id INTEGER NOT NULL REFERENCES file_chunks(id) ON DELETE CASCADE,
                provider VARCHAR(64) NOT NULL,
                model VARCHAR(255) NOT NULL,
                model_revision VARCHAR(64) NOT NULL,
                dimension INTEGER NOT NULL CONSTRAINT ck_late_embedding_dimension CHECK (dimension > 0),
                token_count INTEGER NOT NULL CONSTRAINT ck_late_embedding_token_count CHECK (token_count > 0),
                vector_dtype VARCHAR(16) NOT NULL DEFAULT 'float16',
                vector_data BYTEA NOT NULL,
                content_hash VARCHAR(64) NOT NULL,
                was_truncated BOOLEAN NOT NULL DEFAULT false,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                CONSTRAINT uq_late_embedding_chunk_model_revision
                    UNIQUE (chunk_id, model, model_revision)
            )
            """
        )
    )
    await conn.execute(
        text(
            "ALTER TABLE late_interaction_embeddings "
            "ADD COLUMN IF NOT EXISTS was_truncated BOOLEAN NOT NULL DEFAULT false"
        )
    )
    await conn.execute(
        text(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'ck_late_embedding_dimension'
                      AND conrelid = 'late_interaction_embeddings'::regclass
                ) THEN
                    ALTER TABLE late_interaction_embeddings
                    ADD CONSTRAINT ck_late_embedding_dimension CHECK (dimension > 0);
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'ck_late_embedding_token_count'
                      AND conrelid = 'late_interaction_embeddings'::regclass
                ) THEN
                    ALTER TABLE late_interaction_embeddings
                    ADD CONSTRAINT ck_late_embedding_token_count CHECK (token_count > 0);
                END IF;
            END
            $$;
            """
        )
    )
    await conn.execute(
        text(
            "ALTER TABLE late_interaction_embeddings "
            "ALTER COLUMN vector_dtype SET DEFAULT 'float16'"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_late_interaction_embeddings_repository_id "
            "ON late_interaction_embeddings (repository_id)"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_late_interaction_embeddings_chunk_id "
            "ON late_interaction_embeddings (chunk_id)"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_late_interaction_embeddings_coverage "
            "ON late_interaction_embeddings (repository_id, model, model_revision)"
        )
    )


async def _ensure_late_interaction_shadow_table(conn: AsyncConnection) -> None:
    """Create durable, query-redacted shadow-ranking telemetry."""
    await conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS late_interaction_shadow_events (
                id SERIAL PRIMARY KEY,
                repository_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
                query_hash VARCHAR(64) NOT NULL,
                idempotency_key VARCHAR(64) NOT NULL
                    CONSTRAINT uq_late_shadow_idempotency_key UNIQUE,
                language VARCHAR(32) NOT NULL,
                query_class VARCHAR(64) NOT NULL,
                baseline_final_top_k JSON NOT NULL,
                counterfactual_final_top_k JSON NOT NULL,
                score_metrics JSON NOT NULL,
                status VARCHAR(32) NOT NULL,
                coverage DOUBLE PRECISION NOT NULL
                    CONSTRAINT ck_late_shadow_coverage CHECK (coverage >= 0 AND coverage <= 1),
                timing_ms JSON NOT NULL,
                model_revision VARCHAR(255) NOT NULL,
                index_revision VARCHAR(255) NOT NULL,
                skip_reason VARCHAR(512),
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_late_shadow_created
            ON late_interaction_shadow_events (created_at DESC)
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_late_shadow_repository_created
            ON late_interaction_shadow_events (repository_id, created_at DESC)
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_late_shadow_status_created
            ON late_interaction_shadow_events (status, created_at DESC)
            """
        )
    )


async def _ensure_pgvector_extension(conn: AsyncConnection) -> bool:
    try:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        return True
    except Exception as exc:
        logger.warning(f"pgvector extension unavailable: {exc}")
        return False


async def _current_vector_column_type(conn: AsyncConnection) -> tuple[str | None, int | None]:
    try:
        row = (
            await conn.execute(
                text(
                    """
                    SELECT format_type(a.atttypid, a.atttypmod) AS col_type,
                           a.atttypmod
                    FROM pg_attribute a
                    JOIN pg_class c ON a.attrelid = c.oid
                    JOIN pg_type t ON a.atttypid = t.oid
                    WHERE c.relname = 'embeddings'
                      AND a.attname = 'embedding'
                      AND NOT a.attisdropped
                    """
                )
            )
        ).first()
        if not row or row[0] is None:
            return None, None
        col_type = str(row[0])
        typmod = int(row[1]) if row[1] is not None else None
        return col_type, typmod
    except Exception:
        return None, None


async def _ensure_embedding_vector_column(conn: AsyncConnection, dimension: int) -> None:
    target_type = pgvector_column_sql_type(dimension)
    current_type, _ = await _current_vector_column_type(conn)
    if current_type is not None and current_type != target_type:
        logger.warning(
            f"Embedding vector column type {current_type} != configured {target_type}; "
            "recreating column (incompatible pgvector rows will be cleared)."
        )
        for index_name in pgvector_index_names():
            await conn.execute(text(f"DROP INDEX IF EXISTS {index_name}"))
        await conn.execute(text("ALTER TABLE embeddings DROP COLUMN IF EXISTS embedding"))
        await conn.execute(text(f"ALTER TABLE embeddings ADD COLUMN embedding {target_type}"))
    elif current_type is None:
        await conn.execute(text(f"ALTER TABLE embeddings ADD COLUMN IF NOT EXISTS embedding {target_type}"))

    index_dim = pgvector_index_dimension(dimension)
    try:
        async with conn.begin_nested():
            await conn.execute(
                text(
                    f"""
                    UPDATE embeddings
                    SET embedding = (
                        (SELECT jsonb_agg(elem::text::float8 ORDER BY ord)
                         FROM jsonb_array_elements(vector_data::jsonb) WITH ORDINALITY AS t(elem, ord)
                         WHERE ord <= {index_dim})::text
                    )::{target_type},
                        dimension = COALESCE(dimension, {dimension})
                    WHERE embedding IS NULL
                      AND vector_data IS NOT NULL
                      AND jsonb_array_length(vector_data::jsonb) = {dimension}
                    """  # nosec B608
                )
            )
    except Exception as exc:
        logger.warning(f"Could not backfill pgvector embeddings from JSON: {exc}")

    try:
        # NB: the stale-index DROP happens in _ensure_embedding_vector_column,
        # and ONLY when the dimension/type actually changed. On a normal restart
        # the index already exists, so CREATE INDEX IF NOT EXISTS below is a no-op.
        # (Dropping unconditionally here tore down + full-scan-rebuilt the ANN
        # index on every boot — minutes of degraded search on a populated table.)
        ops = pgvector_distance_ops(dimension)
        # Savepoint: a failed build must not abort the enclosing migration
        # transaction, which would roll back the column swap above with it.
        async with conn.begin_nested():
            await _create_pgvector_index(conn, dimension, ops)
    except Exception as exc:
        logger.warning(f"Could not create pgvector index: {exc}")


async def _create_pgvector_index(conn: AsyncConnection, dimension: int, ops: str) -> None:
    # Parallel index builds allocate maintenance_work_mem in /dev/shm, which is
    # only 64 MB in a default Docker container. A serial build uses ordinary
    # backend memory instead.
    await conn.execute(text("SET LOCAL max_parallel_maintenance_workers = 0"))
    if dimension <= PGVECTOR_VECTOR_MAX_DIM:
        await conn.execute(
            text(
                f"""
                CREATE INDEX IF NOT EXISTS idx_embeddings_vector_cosine
                ON embeddings
                USING ivfflat (embedding {ops})
                WITH (lists = 100)
                WHERE embedding IS NOT NULL
                  AND dimension = {dimension}
                """
            )
        )
    else:
        await conn.execute(
            text(
                f"""
                CREATE INDEX IF NOT EXISTS idx_embeddings_vector_hnsw_half
                ON embeddings
                USING hnsw (embedding {ops})
                WHERE embedding IS NOT NULL
                  AND dimension = {dimension}
                """
            )
        )
