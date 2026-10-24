"""Knowledge-domain upgrades: decision/rule repo scope, uniqueness, context packs, usage events."""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection
from brain.search.filters import HISTORICAL_AUTHORITY_NOTE, HISTORICAL_SUMMARY_PREFIX, NON_CURRENT_PATH_PREFIXES


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


async def _ensure_decision_title_uniqueness(conn: AsyncConnection) -> None:
    """Canonical global scopes agree; duplicate history retains IDs/content."""
    if conn.dialect.name == "sqlite":
        columns = (await conn.execute(text("PRAGMA table_info(decisions)"))).fetchall()
        if "superseded_by_id" not in {row[1] for row in columns}:
            await conn.execute(text("ALTER TABLE decisions ADD COLUMN superseded_by_id INTEGER"))
    else:
        await conn.execute(text("ALTER TABLE decisions ADD COLUMN IF NOT EXISTS superseded_by_id INTEGER"))
    await conn.execute(text("DROP INDEX IF EXISTS uq_decisions_normalized_title"))
    scope = """CASE WHEN repo_path IS NULL OR trim(repo_path)='system_global' THEN ''
             WHEN substr(trim(repo_path),2,1)=':' OR substr(trim(repo_path),1,2)='//'
             THEN lower(trim(repo_path)) ELSE trim(repo_path) END"""
    await conn.execute(text(f"""
        WITH canonical AS (
            SELECT id, MAX(id) OVER (PARTITION BY lower(trim(title)), {scope}) AS winner
            FROM decisions WHERE superseded_by_id IS NULL
        )
        UPDATE decisions SET status='superseded',
            superseded_by_id=(SELECT winner FROM canonical WHERE canonical.id=decisions.id)
        WHERE id IN (SELECT id FROM canonical WHERE id<>winner)
    """))
    await conn.execute(text(f"""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_decisions_normalized_title
        ON decisions (lower(trim(title)), ({scope}))
        WHERE superseded_by_id IS NULL
    """))


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
            """  # nosec B608
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
            """  # nosec B608
        ),
        params,
    )


async def _ensure_context_pack_composition_columns(conn: AsyncConnection) -> None:
    """What each pack delivered, recorded at build time instead of re-parsed
    from the markdown artifact. Existing rows stay NULL on purpose: a null
    means "not measured", and reporting it as zero would understate every
    pack built before this accounting existed."""
    for column, kind in (
        ("budget", "VARCHAR(50)"),
        ("token_estimate", "INTEGER"),
        ("file_count", "INTEGER"),
        ("symbol_count", "INTEGER"),
        ("rule_count", "INTEGER"),
        ("decision_count", "INTEGER"),
        ("learning_count", "INTEGER"),
        ("consumer", "VARCHAR(255)"),
    ):
        await conn.execute(text(f"ALTER TABLE context_packs ADD COLUMN IF NOT EXISTS {column} {kind}"))
    await conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_context_packs_consumer ON context_packs (consumer)")
    )
    await conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_context_packs_created_at ON context_packs (created_at)")
    )


async def _ensure_rule_usage_events_table(conn: AsyncConnection) -> None:
    """Durable, payload-free record of rules reaching agents and catching
    changes, so "used in the last 30 days" is a window and not a lifetime sum."""
    await conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS rule_usage_events (
                id BIGSERIAL PRIMARY KEY,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                rule_id VARCHAR(255) NOT NULL,
                kind VARCHAR(16) NOT NULL,
                repository_id INTEGER,
                context_pack_id INTEGER,
                principal_name VARCHAR(128)
            )
            """
        )
    )
    for name, columns in (
        ("ix_rule_usage_events_rule_id", "(rule_id)"),
        ("ix_rule_usage_events_kind", "(kind)"),
        ("ix_rule_usage_events_created_at", "(created_at)"),
        ("ix_rule_usage_events_window", "(rule_id, kind, created_at)"),
    ):
        await conn.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON rule_usage_events {columns}"))
