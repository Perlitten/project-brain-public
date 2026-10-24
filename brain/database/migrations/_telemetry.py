"""Telemetry/observability tables: insights, request telemetry, tool calls, outcomes, audit."""
from __future__ import annotations

from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


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


async def _ensure_audit_request_id_column(conn: AsyncConnection) -> None:
    for ddl in (
        "ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS request_id VARCHAR(64)",
        "CREATE INDEX IF NOT EXISTS ix_audit_events_request_id ON audit_events (request_id)",
    ):
        try:
            await conn.execute(text(ddl))
        except Exception as exc:
            logger.warning(f"Could not apply audit request_id migration ({ddl}): {exc}")


async def _ensure_request_telemetry_table(conn: AsyncConnection) -> None:
    await conn.execute(text("""
        CREATE TABLE IF NOT EXISTS brain_request_telemetry (
            id BIGSERIAL PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            operation VARCHAR(32) NOT NULL,
            surface VARCHAR(16) NOT NULL DEFAULT 'http',
            repository_id INTEGER NULL,
            repository_path VARCHAR(1024) NULL,
            principal_name VARCHAR(128) NOT NULL,
            request_id VARCHAR(64) NULL,
            outcome VARCHAR(32) NOT NULL,
            latency_ms DOUBLE PRECISION NOT NULL
        )
    """))
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_brain_request_telemetry_created_at ON brain_request_telemetry (created_at)"))
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_brain_request_telemetry_operation ON brain_request_telemetry (operation)"))


    await conn.execute(text("ALTER TABLE brain_request_telemetry ADD COLUMN IF NOT EXISTS surface VARCHAR(16) NOT NULL DEFAULT 'http'"))
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_brain_request_telemetry_repo_time ON brain_request_telemetry (repository_id, created_at)"))


async def _ensure_mcp_tool_calls_table(conn: AsyncConnection) -> None:
    await conn.execute(text("""
        CREATE TABLE IF NOT EXISTS mcp_tool_calls (
            id BIGSERIAL PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            tool VARCHAR(64) NOT NULL,
            surface VARCHAR(16) NOT NULL,
            outcome VARCHAR(16) NOT NULL,
            latency_ms DOUBLE PRECISION NOT NULL
        )
    """))
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_mcp_tool_calls_created_at ON mcp_tool_calls (created_at)"))
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_mcp_tool_calls_tool ON mcp_tool_calls (tool)"))


async def _ensure_skill_outcomes_table(conn: AsyncConnection) -> None:
    await conn.execute(text("""
        CREATE TABLE IF NOT EXISTS memory_skill_outcomes (
            id BIGSERIAL PRIMARY KEY,
            skill_id BIGINT NOT NULL REFERENCES memory_skills(id) ON DELETE CASCADE,
            task_id UUID NOT NULL REFERENCES harness.agent_tasks(id) ON DELETE CASCADE,
            validation_id BIGINT NOT NULL REFERENCES harness.agent_validation_results(id) ON DELETE RESTRICT,
            outcome VARCHAR(16) NOT NULL CHECK (outcome IN ('success','failure')),
            evidence JSONB NOT NULL DEFAULT '{}',
            reported_validation BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_memory_skill_outcome_task_skill UNIQUE (task_id, skill_id)
        )
    """))


async def _ensure_telemetry_state_table(conn: AsyncConnection) -> None:
    await conn.execute(text("""
        CREATE TABLE IF NOT EXISTS brain_telemetry_state (
            id SMALLINT PRIMARY KEY,
            started_at TIMESTAMPTZ NOT NULL
        )
    """))
    await conn.execute(text("""
        INSERT INTO brain_telemetry_state (id, started_at)
        VALUES (1, now()) ON CONFLICT (id) DO NOTHING
    """))
