"""Lightweight schema migrations run during init_db."""

from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from brain.embeddings.constants import EMBEDDING_DIMENSION, resolve_embedding_dimension
from brain.embeddings.pgvector_sql import (
    PGVECTOR_VECTOR_MAX_DIM,
    pgvector_column_sql_type,
    pgvector_distance_ops,
    pgvector_index_dimension,
    pgvector_index_names,
)
from brain.memory.repo_scope import normalize_repo_scope
from brain.search.filters import (
    HISTORICAL_AUTHORITY_NOTE,
    HISTORICAL_SUMMARY_PREFIX,
    NON_CURRENT_PATH_PREFIXES,
)


SCHEMA_MIGRATION_LOCK_ID = 8675309


async def apply_migrations(conn: AsyncConnection, *, acquire_lock: bool = True) -> None:
    """Apply idempotent PostgreSQL schema upgrades."""
    dimension = resolve_embedding_dimension(strict=True)
    if acquire_lock:
        # Transaction-scoped lock releases automatically on commit and rollback,
        # so a failed startup cannot strand a session-level migration lock.
        await conn.execute(text(f"SELECT pg_advisory_xact_lock({SCHEMA_MIGRATION_LOCK_ID})"))
    await _cleanup_legacy_memory_probes(conn)
    await _ensure_decision_repo_scope(conn)
    await _ensure_rule_repo_scope(conn)
    await _ensure_files_path_uniqueness(conn)
    await _ensure_decision_title_uniqueness(conn)
    await _backfill_non_current_knowledge_authority(conn)
    await _ensure_insights_table(conn)
    await _ensure_harness_reliability_columns(conn)
    await _ensure_indexing_progress_columns(conn)
    await _ensure_embedding_metadata_columns(conn)
    await _ensure_late_interaction_table(conn)
    await _ensure_late_interaction_shadow_table(conn)
    await _ensure_indexing_run_file_counts(conn)
    await _ensure_context_pack_repo_scope(conn)
    await _ensure_repository_and_symbol_indexes(conn)
    await _ensure_audit_request_id_column(conn)
    await _ensure_memory_learnings_table(conn)
    await _backfill_memory_repo_scope(conn)
    pgvector_ready = await _ensure_pgvector_extension(conn)
    if pgvector_ready:
        await _ensure_embedding_vector_column(conn, dimension)


async def _ensure_embedding_column(conn: AsyncConnection, table: str) -> None:
    """Add the pgvector ``embedding`` column to a memory table (memory_learnings,
    memory_episodes, memory_skills all repeat this block)."""
    vec_type = pgvector_column_sql_type(EMBEDDING_DIMENSION)
    await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS embedding {vec_type}"))


async def _ensure_indexing_progress_columns(conn: AsyncConnection) -> None:
    await conn.execute(text("ALTER TABLE indexing_runs ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ"))
    await conn.execute(text("ALTER TABLE indexing_runs ADD COLUMN IF NOT EXISTS progress JSON"))
    await conn.execute(text("ALTER TABLE indexing_runs ADD COLUMN IF NOT EXISTS verification JSON"))


async def _ensure_context_pack_repo_scope(conn: AsyncConnection) -> None:
    """Provenance columns on context_packs: which repository and which index
    revision a pack's evidence came from. Nullable — rows written before this
    migration stay unattributed."""
    await conn.execute(
        text("ALTER TABLE context_packs ADD COLUMN IF NOT EXISTS repository_id INTEGER")
    )
    await conn.execute(
        text("ALTER TABLE context_packs ADD COLUMN IF NOT EXISTS repo_commit VARCHAR(255)")
    )
    await conn.execute(
        text(
            """
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conrelid = 'context_packs'::regclass
                      AND conname = 'context_packs_repository_id_fkey'
                ) THEN
                    ALTER TABLE context_packs
                    ADD CONSTRAINT context_packs_repository_id_fkey
                    FOREIGN KEY (repository_id) REFERENCES repositories(id)
                    ON DELETE CASCADE;
                END IF;
            END $$
            """
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_context_packs_repository_id "
            "ON context_packs (repository_id)"
        )
    )


async def _cleanup_legacy_memory_probes(conn: AsyncConnection) -> None:
    """Remove transport-test rows that predate idempotent decision writes."""
    await conn.execute(
        text(
            """
            DELETE FROM decisions
            WHERE title LIKE 'MCP stdio E2E probe%'
               OR title LIKE 'E2E memory probe%'
            """
        )
    )


async def _ensure_decision_repo_scope(conn: AsyncConnection) -> None:
    """Add optional repository ownership to normative decision memory."""
    await conn.execute(
        text(
            """
            ALTER TABLE decisions
            ADD COLUMN IF NOT EXISTS repo_path VARCHAR(1024)
            """
        )
    )


async def _ensure_rule_repo_scope(conn: AsyncConnection) -> None:
    """Add optional repository ownership to normative rule memory."""
    await conn.execute(
        text(
            """
            ALTER TABLE rules
            ADD COLUMN IF NOT EXISTS repo_path VARCHAR(1024)
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_rules_repo_path
            ON rules (repo_path)
            """
        )
    )


async def _ensure_files_path_uniqueness(conn: AsyncConnection) -> None:
    """One canonical files row per (repository_id, path).

    The file indexer's read-then-write upsert could race and insert duplicate
    rows for the same path; every later reindex of such a file then failed
    with MultipleResultsFound and the whole run degraded to stale_blocked.
    Dedupe first (keep the latest row; FK cascades clean up chunks/symbols),
    then enforce uniqueness.
    """
    await conn.execute(
        text(
            """
            DELETE FROM files WHERE id IN (
                SELECT id FROM (
                    SELECT id, ROW_NUMBER() OVER (
                        PARTITION BY repository_id, path ORDER BY id DESC
                    ) AS rn FROM files
                ) s WHERE rn > 1
            )
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_files_repository_path
            ON files (repository_id, path)
            """
        )
    )


async def _ensure_decision_title_uniqueness(conn: AsyncConnection) -> None:
    """Enforce one canonical decision title per repository scope."""
    await conn.execute(text("DROP INDEX IF EXISTS uq_decisions_normalized_title"))
    await conn.execute(
        text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_decisions_normalized_title
            ON decisions (
                lower(btrim(title)),
                COALESCE(lower(btrim(repo_path)), '')
            )
            """
        )
    )


async def _backfill_non_current_knowledge_authority(
    conn: AsyncConnection,
) -> None:
    """Annotate already-indexed snapshots without re-embedding their content."""
    path_predicate = " OR ".join(f"path LIKE '{prefix}%'" for prefix in NON_CURRENT_PATH_PREFIXES)
    file_path_predicate = " OR ".join(f"file.path LIKE '{prefix}%'" for prefix in NON_CURRENT_PATH_PREFIXES)
    prefix_text = f"{HISTORICAL_SUMMARY_PREFIX} {HISTORICAL_AUTHORITY_NOTE}"
    params = {
        "prefix_text": prefix_text,
        "prefix_like": f"{HISTORICAL_SUMMARY_PREFIX}%",
    }
    await conn.execute(
        text(
            f"""
            UPDATE files
            SET summary = btrim(:prefix_text || ' ' || COALESCE(summary, ''))
            WHERE ({path_predicate})
              AND (summary IS NULL OR ltrim(summary) NOT LIKE :prefix_like)
            """
        ),
        params,
    )
    await conn.execute(
        text(
            f"""
            UPDATE file_chunks AS chunk
            SET summary = btrim(:prefix_text || ' ' || COALESCE(chunk.summary, ''))
            FROM files AS file
            WHERE chunk.file_id = file.id
              AND ({file_path_predicate})
              AND (
                  chunk.summary IS NULL
                  OR ltrim(chunk.summary) NOT LIKE :prefix_like
              )
            """
        ),
        params,
    )


async def _ensure_insights_table(conn: AsyncConnection) -> None:
    """Create the proactive insight inbox on existing installs.

    Fresh databases get this from SQLAlchemy metadata before migrations run; this
    idempotent DDL keeps older self-hosted installs upgradeable by restart.
    """
    try:
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS insights (
                    id SERIAL PRIMARY KEY,
                    insight_type VARCHAR(100) NOT NULL,
                    severity VARCHAR(50) NOT NULL,
                    title VARCHAR(255) NOT NULL,
                    summary TEXT NOT NULL,
                    evidence JSON NOT NULL,
                    recommended_action TEXT,
                    confidence VARCHAR(50) NOT NULL DEFAULT 'medium',
                    dedupe_key VARCHAR(255) NOT NULL UNIQUE,
                    status VARCHAR(50) NOT NULL DEFAULT 'new',
                    source VARCHAR(50) NOT NULL DEFAULT 'deterministic',
                    source_model VARCHAR(128),
                    engine_version VARCHAR(64) NOT NULL DEFAULT 'p0.1',
                    occurrence_count INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        )
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_insights_id ON insights (id)"))
        await conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_insights_dedupe_key ON insights (dedupe_key)"))
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_insights_status_created ON insights (status, created_at DESC)")
        )
    except Exception as exc:
        logger.warning(f"Could not apply insights table migration: {exc}")


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


async def _ensure_indexing_run_file_counts(conn: AsyncConnection) -> None:
    try:
        await conn.execute(
            text("ALTER TABLE indexing_runs ADD COLUMN IF NOT EXISTS file_counts JSON")
        )
    except Exception as exc:
        logger.warning(f"Could not apply indexing_runs.file_counts migration: {exc}")


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


async def _ensure_harness_reliability_columns(conn: AsyncConnection) -> None:
    """Upgrade the harness ledger to server-enforced idempotency and CAS."""
    ddl_statements = (
        "ALTER TABLE harness.agent_tasks ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE harness.agent_task_events ADD COLUMN IF NOT EXISTS classification VARCHAR(32) NOT NULL DEFAULT 'observation'",
        "ALTER TABLE harness.agent_task_events ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(255)",
        "ALTER TABLE harness.agent_task_events ADD COLUMN IF NOT EXISTS causal_parent_id BIGINT",
        "ALTER TABLE harness.agent_task_events ADD COLUMN IF NOT EXISTS expected_task_version INTEGER",
        "ALTER TABLE harness.agent_task_events ADD COLUMN IF NOT EXISTS task_version INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE harness.worker_leases ADD COLUMN IF NOT EXISTS fencing_token BIGINT NOT NULL DEFAULT 1",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_task_events_task_idempotency "
        "ON harness.agent_task_events (task_id, idempotency_key) "
        "WHERE idempotency_key IS NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_worker_leases_task ON harness.worker_leases (task_id)",
    )
    for ddl in ddl_statements:
        # These constraints define the correctness boundary for event replay and
        # writer fencing. Starting without any one of them would advertise CAS
        # while silently running in the old unsafe mode, so migration failure is
        # intentionally fatal.
        await conn.execute(text(ddl))


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
                    """
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


async def _ensure_repository_and_symbol_indexes(conn: AsyncConnection) -> None:
    """Add missing FK/query indexes for repository-scoped lookups."""
    ddl_statements = (
        "CREATE INDEX IF NOT EXISTS ix_files_repository_path ON files (repository_id, path)",
        "CREATE INDEX IF NOT EXISTS ix_files_path ON files (path)",
        "CREATE INDEX IF NOT EXISTS ix_symbols_file_id ON symbols (file_id)",
        "CREATE INDEX IF NOT EXISTS ix_file_chunks_file_id ON file_chunks (file_id)",
        "CREATE INDEX IF NOT EXISTS ix_file_cards_repository_id ON file_cards (repository_id)",
        "CREATE INDEX IF NOT EXISTS ix_file_cards_file_id ON file_cards (file_id)",
        "CREATE INDEX IF NOT EXISTS ix_indexing_runs_repository_id ON indexing_runs (repository_id)",
    )
    for ddl in ddl_statements:
        try:
            await conn.execute(text(ddl))
        except Exception as exc:
            logger.warning(f"Could not create index ({ddl}): {exc}")


async def _ensure_audit_request_id_column(conn: AsyncConnection) -> None:
    for ddl in (
        "ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS request_id VARCHAR(64)",
        "CREATE INDEX IF NOT EXISTS ix_audit_events_request_id ON audit_events (request_id)",
    ):
        try:
            await conn.execute(text(ddl))
        except Exception as exc:
            logger.warning(f"Could not apply audit request_id migration ({ddl}): {exc}")


async def _ensure_memory_learnings_table(conn: AsyncConnection) -> None:
    """Create the L3 semantic memory table for consolidated learnings.

    Fresh databases get this from SQLAlchemy metadata before migrations run;
    this idempotent DDL keeps older self-hosted installs upgradeable by restart.
    """
    try:
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS memory_learnings (
                    id BIGSERIAL PRIMARY KEY,
                    statement TEXT NOT NULL,
                    category VARCHAR(64),
                    confidence FLOAT NOT NULL DEFAULT 0.5,
                    evidence JSONB NOT NULL DEFAULT '[]',
                    status VARCHAR(16) NOT NULL DEFAULT 'active',
                    superseded_by BIGINT REFERENCES memory_learnings(id) ON DELETE SET NULL,
                    valid_until TIMESTAMPTZ,
                    repo_scope VARCHAR(1024),
                    promoted_from VARCHAR(64),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        )
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_memory_learnings_status ON memory_learnings (status)"))
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_memory_learnings_status_scope ON memory_learnings (status, repo_scope)")
        )
        await _ensure_embedding_column(conn, "memory_learnings")
        await conn.execute(
            text("ALTER TABLE memory_learnings ADD COLUMN IF NOT EXISTS embedding_model VARCHAR(128)")
        )
    except Exception as exc:
        logger.warning(f"Could not apply memory_learnings table migration: {exc}")

    # L2 episodic memory: clustered, distilled episodes (industry-standard 4-tier).
    try:
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS memory_episodes (
                    id BIGSERIAL PRIMARY KEY,
                    source_event_ids JSONB NOT NULL DEFAULT '[]',
                    distilled_summary TEXT NOT NULL,
                    topic VARCHAR(128),
                    status VARCHAR(16) NOT NULL DEFAULT 'pending',
                    promoted_to_learning_id BIGINT REFERENCES memory_learnings(id) ON DELETE SET NULL,
                    merged_into_episode_id BIGINT REFERENCES memory_episodes(id) ON DELETE SET NULL,
                    confidence FLOAT NOT NULL DEFAULT 0.5,
                    repo_scope VARCHAR(1024),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        )
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_memory_episodes_status ON memory_episodes (status)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_memory_episodes_topic ON memory_episodes (topic)"))
        await _ensure_embedding_column(conn, "memory_episodes")
    except Exception as exc:
        logger.warning(f"Could not apply memory_episodes table migration: {exc}")

    await _ensure_memory_episode_ledger(conn)

    # L4 procedural memory: skills registry (industry-standard 4-tier).
    try:
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS memory_skills (
                    id BIGSERIAL PRIMARY KEY,
                    name VARCHAR(128) NOT NULL UNIQUE,
                    description TEXT NOT NULL,
                    triggers JSONB NOT NULL DEFAULT '[]',
                    workflow JSONB NOT NULL DEFAULT '[]',
                    source_episode_ids JSONB NOT NULL DEFAULT '[]',
                    source_learning_ids JSONB NOT NULL DEFAULT '[]',
                    times_used INTEGER NOT NULL DEFAULT 0,
                    times_successful INTEGER NOT NULL DEFAULT 0,
                    status VARCHAR(16) NOT NULL DEFAULT 'active',
                    confidence FLOAT NOT NULL DEFAULT 0.5,
                    repo_scope VARCHAR(1024),
                    is_global BOOLEAN NOT NULL DEFAULT false,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        )
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_memory_skills_status ON memory_skills (status)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_memory_skills_name ON memory_skills (name)"))
        await _ensure_embedding_column(conn, "memory_skills")
        # Explicit global marker for context injection; a NULL repo_scope is
        # not global and is never injected (underivable backfills stay NULL).
        await conn.execute(
            text("ALTER TABLE memory_skills ADD COLUMN IF NOT EXISTS is_global BOOLEAN NOT NULL DEFAULT false")
        )
    except Exception as exc:
        logger.warning(f"Could not apply memory_skills table migration: {exc}")


async def _ensure_memory_episode_ledger(conn: AsyncConnection) -> None:
    """Gate-outcome columns on memory_episodes + the L1 consumption ledger.

    Additive only; reversed by ``downgrade_memory_episode_ledger``.
    """
    try:
        await conn.execute(
            text(
                "ALTER TABLE memory_episodes ADD COLUMN IF NOT EXISTS duplicate_of_learning_id BIGINT "
                "REFERENCES memory_learnings(id) ON DELETE SET NULL"
            )
        )
        await conn.execute(
            text("ALTER TABLE memory_episodes ADD COLUMN IF NOT EXISTS gate_reasons JSONB NOT NULL DEFAULT '[]'")
        )
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS memory_episode_events (
                    event_id BIGINT PRIMARY KEY,
                    episode_id BIGINT NOT NULL REFERENCES memory_episodes(id) ON DELETE CASCADE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_memory_episode_events_episode_id "
                "ON memory_episode_events (episode_id)"
            )
        )
    except Exception as exc:
        logger.warning(f"Could not apply memory_episode ledger migration: {exc}")


async def _backfill_memory_repo_scope(conn: AsyncConnection) -> None:
    """Populate repo_scope on existing L2 episodes and L4 skills where derivable.

    Episodes derive their scope from their L1 source events
    (``source_event_ids`` → ``harness.agent_task_events`` →
    ``harness.agent_tasks.repo_path``), taking the modal repo when the cluster
    mixed repositories. Skills derive from their ``source_episode_ids``.
    Rows with no derivable scope stay NULL (= global scope) — that is
    intentional, not a gap: an episode built from deleted tasks or a manually
    registered skill has no provable repository, and NULL is the honest answer.
    """
    # Each half runs in its own savepoint: a failure rolls back the backfill
    # only, not the outer migration transaction (Postgres aborts the whole
    # transaction on any failed statement).
    try:
        async with conn.begin_nested():
            rows = (
                await conn.execute(
                    text(
                        """
                        SELECT ep.id, t.repo_path, COUNT(*) AS n
                        FROM memory_episodes ep
                        CROSS JOIN LATERAL jsonb_array_elements_text(ep.source_event_ids::jsonb) AS ev(eid)
                        JOIN harness.agent_task_events e ON e.id = ev.eid::bigint
                        JOIN harness.agent_tasks t ON t.id = e.task_id
                        WHERE ep.repo_scope IS NULL
                        GROUP BY ep.id, t.repo_path
                        ORDER BY ep.id, n DESC, t.repo_path
                        """
                    )
                )
            ).all()
            best: dict[int, str] = {}
            for episode_id, repo_path, _n in rows:
                best.setdefault(int(episode_id), repo_path)
            for episode_id, repo_path in best.items():
                await conn.execute(
                    text("UPDATE memory_episodes SET repo_scope = :scope WHERE id = :id"),
                    {"scope": normalize_repo_scope(repo_path), "id": episode_id},
                )
    except Exception as exc:
        logger.warning(f"Could not backfill memory_episodes repo_scope: {exc}")

    try:
        async with conn.begin_nested():
            rows = (
                await conn.execute(
                    text(
                        """
                        SELECT s.id, ep.repo_scope, COUNT(*) AS n
                        FROM memory_skills s
                        CROSS JOIN LATERAL jsonb_array_elements_text(s.source_episode_ids::jsonb) AS ev(eid)
                        JOIN memory_episodes ep ON ep.id = ev.eid::bigint
                        WHERE s.repo_scope IS NULL AND ep.repo_scope IS NOT NULL
                        GROUP BY s.id, ep.repo_scope
                        ORDER BY s.id, n DESC, ep.repo_scope
                        """
                    )
                )
            ).all()
            best_skill: dict[int, str] = {}
            for skill_id, scope, _n in rows:
                best_skill.setdefault(int(skill_id), scope)
            for skill_id, scope in best_skill.items():
                await conn.execute(
                    text("UPDATE memory_skills SET repo_scope = :scope WHERE id = :id"),
                    {"scope": normalize_repo_scope(scope), "id": skill_id},
                )
    except Exception as exc:
        logger.warning(f"Could not backfill memory_skills repo_scope: {exc}")


async def downgrade_memory_episode_ledger(conn: AsyncConnection) -> None:
    """Reverse ``_ensure_memory_episode_ledger`` (manual rollback; data in these objects is lost)."""
    await conn.execute(text("DROP TABLE IF EXISTS memory_episode_events"))
    await conn.execute(text("ALTER TABLE memory_episodes DROP COLUMN IF EXISTS gate_reasons"))
    await conn.execute(text("ALTER TABLE memory_episodes DROP COLUMN IF EXISTS duplicate_of_learning_id"))
