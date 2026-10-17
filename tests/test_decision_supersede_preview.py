"""``deploy/sql/preview_decision_supersede.sql`` predicts exactly what the decision
title migration supersedes, on a table shaped like the c13d056 production schema."""
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from brain.database.migrations import _ensure_decision_title_uniqueness

PREVIEW = Path(__file__).resolve().parents[1] / "deploy" / "sql" / "preview_decision_supersede.sql"
ROWS = [  # (id, title, repo_path)
    (1, "Payment policy", None),
    (2, "payment policy ", "system_global"),  # same global scope as 1 -> 1 is superseded
    (3, "Payment policy", "/CaseRepo"),
    (4, "Payment policy", "/caserepo"),  # POSIX paths stay case-distinct
    (5, "Deploy", "C:/Repo"),
    (6, "Deploy", "c:/repo"),  # Windows paths fold case -> 5 is superseded
    (7, "Deploy", " system_global"),
    (8, "Deploy", None),  # -> 7 is superseded
    (9, "Unrelated", "/RepoA"),
]


def _statements():
    sql = "\n".join(line for line in PREVIEW.read_text(encoding="utf-8").splitlines() if not line.startswith("--"))
    return [statement.strip() for statement in sql.split(";") if statement.strip()]


@pytest.mark.asyncio
async def test_preview_matches_what_the_migration_supersedes(tmp_path):
    count_sql, detail_sql = _statements()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'c13d056.sqlite3'}")
    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE TABLE decisions(id INTEGER PRIMARY KEY, title TEXT, "
                                          "repo_path TEXT, status TEXT, description TEXT)"))
            for row_id, title, repo_path in ROWS:
                await connection.execute(text("INSERT INTO decisions VALUES(:id, :title, :repo, 'active', 'd')"),
                                         {"id": row_id, "title": title, "repo": repo_path})
            predicted_count = (await connection.execute(text(count_sql))).scalar_one()
            predicted = {(row.id, row.superseded_by_id) for row in await connection.execute(text(detail_sql))}

            await _ensure_decision_title_uniqueness(connection)
            actual = {tuple(row) for row in await connection.execute(text(
                "SELECT id, superseded_by_id FROM decisions WHERE status='superseded'"))}
    finally:
        await engine.dispose()
    assert predicted == actual == {(1, 2), (5, 6), (7, 8)}
    assert predicted_count == 3
