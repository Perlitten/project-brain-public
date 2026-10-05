"""Tests for the files (repository_id, path) uniqueness migration.

Regression test for the bug where a racy read-then-write upsert in the
file indexer created duplicate `files` rows for the same path; every later
reindex of such a file failed with MultipleResultsFound, the run degraded,
and /context fail-closed with stale_blocked.
"""

from unittest.mock import AsyncMock

import pytest

from brain.database.migrations import _ensure_files_path_uniqueness


@pytest.mark.asyncio
async def test_files_path_uniqueness_dedupes_then_enforces():
    connection = AsyncMock()

    await _ensure_files_path_uniqueness(connection)

    statements = [str(call.args[0]) for call in connection.execute.await_args_list]
    assert len(statements) == 2
    # Dedupe first: keep the latest row per (repository_id, path).
    assert "DELETE FROM files" in statements[0]
    assert "PARTITION BY repository_id, path" in statements[0]
    # Then enforce uniqueness idempotently.
    assert "CREATE UNIQUE INDEX IF NOT EXISTS uq_files_repository_path" in statements[1]
    assert "ON files (repository_id, path)" in statements[1]
