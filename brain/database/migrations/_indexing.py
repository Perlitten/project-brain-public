"""Indexing-schema upgrades: file path uniqueness, progress columns, run counts, indexes."""
from __future__ import annotations

from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def _ensure_indexing_progress_columns(conn: AsyncConnection) -> None:
    await conn.execute(text("ALTER TABLE indexing_runs ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ"))
    await conn.execute(text("ALTER TABLE indexing_runs ADD COLUMN IF NOT EXISTS progress JSON"))
    await conn.execute(text("ALTER TABLE indexing_runs ADD COLUMN IF NOT EXISTS verification JSON"))


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


async def _ensure_indexing_run_file_counts(conn: AsyncConnection) -> None:
    try:
        await conn.execute(
            text("ALTER TABLE indexing_runs ADD COLUMN IF NOT EXISTS file_counts JSON")
        )
    except Exception as exc:
        logger.warning(f"Could not apply indexing_runs.file_counts migration: {exc}")


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
