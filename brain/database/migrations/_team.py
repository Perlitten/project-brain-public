"""Team-domain schema upgrades (projects, members, invitations, execution, harness)."""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def _ensure_team_project_name_uniqueness(conn: AsyncConnection) -> None:
    """Make project creation race-safe on databases created before the constraint existed."""
    await conn.execute(
        text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_team_projects_team_name
            ON team_projects (team_id, name)
            """
        )
    )


async def _ensure_workspace_registry_tables(conn: AsyncConnection) -> None:
    """The team-state workspace registry (``brain.team.registry``): one table every process reads.

    ``create_all`` creates both tables from ``brain.team.models``; this keeps an
    upgraded database that skipped ``create_all`` for them in step, idempotently."""
    if conn.dialect.name == "sqlite":
        return
    await conn.execute(text("""
        CREATE TABLE IF NOT EXISTS workspace_registry_entries (
            repository_id VARCHAR(64) PRIMARY KEY,
            normalized_root_identity VARCHAR(1024) NOT NULL,
            trust_level VARCHAR(32) NOT NULL,
            disabled BOOLEAN NOT NULL DEFAULT FALSE,
            version INTEGER NOT NULL DEFAULT 1,
            record JSON NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """))
    await conn.execute(text("""
        CREATE TABLE IF NOT EXISTS workspace_registry_events (
            id SERIAL PRIMARY KEY,
            event_id VARCHAR(64) NOT NULL UNIQUE,
            repository_id VARCHAR(64) NOT NULL,
            event_type VARCHAR(32) NOT NULL,
            event JSON NOT NULL,
            recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """))
    await conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_workspace_registry_events_repository_id "
        "ON workspace_registry_events (repository_id)"))


async def _ensure_team_dissolved_column(conn: AsyncConnection) -> None:
    """A team keeps its boundaries after the last member is revoked until ``brain team dissolve``."""
    if conn.dialect.name == "sqlite":
        columns = (await conn.execute(text("PRAGMA table_info(teams)"))).fetchall()
        if columns and "dissolved_at" not in {row[1] for row in columns}:
            await conn.execute(text("ALTER TABLE teams ADD COLUMN dissolved_at TIMESTAMP"))
    else:
        await conn.execute(text("ALTER TABLE IF EXISTS teams ADD COLUMN IF NOT EXISTS dissolved_at TIMESTAMPTZ"))


async def _ensure_team_invitation_oidc_columns(conn: AsyncConnection) -> None:
    """An invitation accepted through OIDC records the identity provider and subject that vouched for it."""
    columns = ("verified_issuer", "verified_subject")
    if conn.dialect.name == "sqlite":
        existing = (await conn.execute(text("PRAGMA table_info(team_invitations)"))).fetchall()
        if not existing:
            return
        names = {row[1] for row in existing}
        for column in columns:
            if column not in names:
                await conn.execute(text(f"ALTER TABLE team_invitations ADD COLUMN {column} VARCHAR(255)"))
    else:
        for column in columns:
            await conn.execute(text(
                f"ALTER TABLE IF EXISTS team_invitations ADD COLUMN IF NOT EXISTS {column} VARCHAR(255)"))


async def _ensure_execution_environment_column(conn: AsyncConnection) -> None:
    """A bounded execution records the operator environment (image + tools) it runs in (container backend)."""
    if conn.dialect.name == "sqlite":
        existing = (await conn.execute(text("PRAGMA table_info(agent_run_executions)"))).fetchall()
        if existing and "environment" not in {row[1] for row in existing}:
            await conn.execute(text("ALTER TABLE agent_run_executions ADD COLUMN environment VARCHAR(64)"))
    else:
        await conn.execute(text(
            "ALTER TABLE IF EXISTS harness.agent_run_executions ADD COLUMN IF NOT EXISTS environment VARCHAR(64)"))


async def _ensure_execution_llm_column(conn: AsyncConnection) -> None:
    """A bounded execution records its declared LLM provider, model and token/cost ceiling (NULL: none)."""
    if conn.dialect.name == "sqlite":
        existing = (await conn.execute(text("PRAGMA table_info(agent_run_executions)"))).fetchall()
        if existing and "llm" not in {row[1] for row in existing}:
            await conn.execute(text("ALTER TABLE agent_run_executions ADD COLUMN llm JSON"))
    else:
        await conn.execute(text(
            "ALTER TABLE IF EXISTS harness.agent_run_executions ADD COLUMN IF NOT EXISTS llm JSON"))


async def _ensure_team_identity_session_column(conn: AsyncConnection) -> None:
    """An SSO binding remembers the credential its latest sign-in minted, so the next one revokes it."""
    if conn.dialect.name == "sqlite":
        columns = (await conn.execute(text("PRAGMA table_info(team_identities)"))).fetchall()
        if columns and "session_credential_id" not in {row[1] for row in columns}:
            await conn.execute(text("ALTER TABLE team_identities ADD COLUMN session_credential_id INTEGER"))
    else:
        await conn.execute(text(
            "ALTER TABLE IF EXISTS team_identities ADD COLUMN IF NOT EXISTS session_credential_id INTEGER"))


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
