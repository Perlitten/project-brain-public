"""Memory-tier upgrades: authorship/provenance columns, learnings, episode ledger, scope backfill."""
from __future__ import annotations

from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection
from brain.memory.repo_scope import normalize_repo_scope
from ._embeddings import _ensure_embedding_column


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


async def _ensure_memory_authorship_columns(conn: AsyncConnection) -> None:
    """Decisions and rules remember the principal that recorded them (NULL before this)."""
    for table in ("decisions", "rules"):
        if conn.dialect.name == "sqlite":
            columns = (await conn.execute(text(f"PRAGMA table_info({table})"))).fetchall()
            if "author_principal_id" not in {row[1] for row in columns}:
                await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN author_principal_id INTEGER"))
        else:
            await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS author_principal_id INTEGER"))


# Provenance and supersede columns per memory table (brain/memory/provenance.py).
_MEMORY_PROVENANCE_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "decisions": (
        ("updated_by_principal_id", "INTEGER"),
        ("supersedes_id", "INTEGER"),
        ("version", "INTEGER NOT NULL DEFAULT 1"),
        ("approved_by_principal_id", "INTEGER"),
    ),
    "rules": (
        ("updated_by_principal_id", "INTEGER"),
        ("supersedes_id", "VARCHAR(255)"),
        ("superseded_by_id", "VARCHAR(255)"),
        ("version", "INTEGER NOT NULL DEFAULT 1"),
        ("approved_by_principal_id", "INTEGER"),
    ),
    "memory_learnings": (("author_principal_id", "INTEGER"), ("updated_by_principal_id", "INTEGER"),
                         ("approved_by_principal_id", "INTEGER")),
    "memory_episodes": (("author_principal_id", "INTEGER"), ("updated_by_principal_id", "INTEGER")),
    "memory_skills": (("author_principal_id", "INTEGER"), ("updated_by_principal_id", "INTEGER"),
                      ("approved_by_principal_id", "INTEGER")),
}


async def _ensure_memory_provenance_columns(conn: AsyncConnection) -> None:
    """Every memory object carries author/updated_by; decisions and rules also a version chain.

    Rows that predate this keep NULL authorship and start at version 1."""
    for table, columns in _MEMORY_PROVENANCE_COLUMNS.items():
        if conn.dialect.name == "sqlite":
            existing = {row[1] for row in (await conn.execute(text(f"PRAGMA table_info({table})"))).fetchall()}
            if not existing:
                continue  # table absent under Lite storage
            for column, ddl in columns:
                if column not in existing:
                    await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
        else:
            for column, ddl in columns:
                await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl}"))


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
            text("ALTER TABLE memory_episodes ADD COLUMN IF NOT EXISTS supersedes_learning_ids JSONB")
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
    await conn.execute(text("ALTER TABLE memory_episodes DROP COLUMN IF EXISTS supersedes_learning_ids"))
    await conn.execute(text("ALTER TABLE memory_episodes DROP COLUMN IF EXISTS duplicate_of_learning_id"))
