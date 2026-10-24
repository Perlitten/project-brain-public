"""The ordered migration runner."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection
from brain.embeddings.constants import resolve_embedding_dimension
from ._embeddings import (_ensure_embedding_metadata_columns, _ensure_embedding_vector_column, _ensure_late_interaction_shadow_table, _ensure_late_interaction_table, _ensure_pgvector_extension)
from ._indexing import (_ensure_files_path_uniqueness, _ensure_indexing_progress_columns, _ensure_indexing_run_file_counts, _ensure_repository_and_symbol_indexes)
from ._knowledge import (_backfill_non_current_knowledge_authority, _ensure_context_pack_composition_columns, _ensure_context_pack_repo_scope, _ensure_decision_repo_scope, _ensure_decision_title_uniqueness, _ensure_rule_repo_scope, _ensure_rule_usage_events_table)
from ._memory import (_backfill_memory_repo_scope, _cleanup_legacy_memory_probes, _ensure_memory_authorship_columns, _ensure_memory_learnings_table, _ensure_memory_provenance_columns)
from ._team import (_ensure_execution_environment_column, _ensure_execution_llm_column, _ensure_harness_reliability_columns, _ensure_team_dissolved_column, _ensure_team_identity_session_column, _ensure_team_invitation_oidc_columns, _ensure_team_project_name_uniqueness, _ensure_workspace_registry_tables)
from ._telemetry import (_ensure_audit_request_id_column, _ensure_insights_table, _ensure_mcp_tool_calls_table, _ensure_request_telemetry_table, _ensure_skill_outcomes_table, _ensure_telemetry_state_table)


SCHEMA_MIGRATION_LOCK_ID = 8675309


async def _ensure_migrations_ledger(conn: AsyncConnection) -> None:
    """The schema-version ledger: one row per applied migration step."""
    await conn.execute(text(
        "CREATE TABLE IF NOT EXISTS brain_schema_migrations ("
        "step TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"))


async def _step(conn: AsyncConnection, name: str, fn, *args) -> None:
    """Run one migration step and record it in the ledger."""
    await fn(conn, *args)
    await conn.execute(text(
        "INSERT INTO brain_schema_migrations (step, applied_at) VALUES (:s, :t) ON CONFLICT DO NOTHING"),
        {"s": name, "t": datetime.now(timezone.utc).isoformat()})


async def apply_migrations(conn: AsyncConnection, *, acquire_lock: bool = True) -> None:
    """Apply idempotent PostgreSQL schema upgrades."""
    dimension = resolve_embedding_dimension(strict=True)
    if acquire_lock:
        # Transaction-scoped lock releases automatically on commit and rollback,
        # so a failed startup cannot strand a session-level migration lock.
        await conn.execute(text(f"SELECT pg_advisory_xact_lock({SCHEMA_MIGRATION_LOCK_ID})"))
    await _ensure_migrations_ledger(conn)
    await _cleanup_legacy_memory_probes(conn)
    await _step(conn, "decision_repo_scope", _ensure_decision_repo_scope)
    await _step(conn, "rule_repo_scope", _ensure_rule_repo_scope)
    await _step(conn, "files_path_uniqueness", _ensure_files_path_uniqueness)
    await _step(conn, "team_project_name_uniqueness", _ensure_team_project_name_uniqueness)
    await _step(conn, "decision_title_uniqueness", _ensure_decision_title_uniqueness)
    await _step(conn, "memory_authorship_columns", _ensure_memory_authorship_columns)
    await _step(conn, "team_dissolved_column", _ensure_team_dissolved_column)
    await _step(conn, "team_invitation_oidc_columns", _ensure_team_invitation_oidc_columns)
    await _step(conn, "execution_environment_column", _ensure_execution_environment_column)
    await _step(conn, "execution_llm_column", _ensure_execution_llm_column)
    await _step(conn, "team_identity_session_column", _ensure_team_identity_session_column)
    await _step(conn, "workspace_registry_tables", _ensure_workspace_registry_tables)
    await _step(conn, "non_current_knowledge_authority", _backfill_non_current_knowledge_authority)
    await _step(conn, "insights_table", _ensure_insights_table)
    await _step(conn, "harness_reliability_columns", _ensure_harness_reliability_columns)
    await _step(conn, "indexing_progress_columns", _ensure_indexing_progress_columns)
    await _step(conn, "embedding_metadata_columns", _ensure_embedding_metadata_columns)
    await _step(conn, "late_interaction_table", _ensure_late_interaction_table)
    await _step(conn, "late_interaction_shadow_table", _ensure_late_interaction_shadow_table)
    await _step(conn, "indexing_run_file_counts", _ensure_indexing_run_file_counts)
    await _step(conn, "context_pack_repo_scope", _ensure_context_pack_repo_scope)
    await _step(conn, "context_pack_composition_columns", _ensure_context_pack_composition_columns)
    await _step(conn, "rule_usage_events_table", _ensure_rule_usage_events_table)
    await _step(conn, "repository_and_symbol_indexes", _ensure_repository_and_symbol_indexes)
    await _step(conn, "audit_request_id_column", _ensure_audit_request_id_column)
    await _step(conn, "request_telemetry_table", _ensure_request_telemetry_table)
    await _step(conn, "mcp_tool_calls_table", _ensure_mcp_tool_calls_table)
    await _step(conn, "skill_outcomes_table", _ensure_skill_outcomes_table)
    await _step(conn, "telemetry_state_table", _ensure_telemetry_state_table)
    await _step(conn, "memory_learnings_table", _ensure_memory_learnings_table)
    await _step(conn, "memory_provenance_columns", _ensure_memory_provenance_columns)
    await _step(conn, "memory_repo_scope", _backfill_memory_repo_scope)
    pgvector_ready = await _ensure_pgvector_extension(conn)
    if pgvector_ready:
        await _step(conn, "embedding_vector_column", _ensure_embedding_vector_column, dimension)


async def schema_migrations_view(conn: AsyncConnection) -> list[dict]:
    """What schema steps have been applied and when — for doctor/status output."""
    await _ensure_migrations_ledger(conn)
    rows = (await conn.execute(text(
        "SELECT step, applied_at FROM brain_schema_migrations ORDER BY applied_at"))).all()
    return [{"step": step, "applied_at": applied_at} for step, applied_at in rows]
