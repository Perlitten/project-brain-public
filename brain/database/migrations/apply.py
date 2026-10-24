"""The ordered migration runner."""
from __future__ import annotations

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
    await _ensure_team_project_name_uniqueness(conn)
    await _ensure_decision_title_uniqueness(conn)
    await _ensure_memory_authorship_columns(conn)
    await _ensure_team_dissolved_column(conn)
    await _ensure_team_invitation_oidc_columns(conn)
    await _ensure_execution_environment_column(conn)
    await _ensure_execution_llm_column(conn)
    await _ensure_team_identity_session_column(conn)
    await _ensure_workspace_registry_tables(conn)
    await _backfill_non_current_knowledge_authority(conn)
    await _ensure_insights_table(conn)
    await _ensure_harness_reliability_columns(conn)
    await _ensure_indexing_progress_columns(conn)
    await _ensure_embedding_metadata_columns(conn)
    await _ensure_late_interaction_table(conn)
    await _ensure_late_interaction_shadow_table(conn)
    await _ensure_indexing_run_file_counts(conn)
    await _ensure_context_pack_repo_scope(conn)
    await _ensure_context_pack_composition_columns(conn)
    await _ensure_rule_usage_events_table(conn)
    await _ensure_repository_and_symbol_indexes(conn)
    await _ensure_audit_request_id_column(conn)
    await _ensure_request_telemetry_table(conn)
    await _ensure_mcp_tool_calls_table(conn)
    await _ensure_skill_outcomes_table(conn)
    await _ensure_telemetry_state_table(conn)
    await _ensure_memory_learnings_table(conn)
    await _ensure_memory_provenance_columns(conn)
    await _backfill_memory_repo_scope(conn)
    pgvector_ready = await _ensure_pgvector_extension(conn)
    if pgvector_ready:
        await _ensure_embedding_vector_column(conn, dimension)
