"""CI gate: fresh-database init + migration idempotency.

Runs the production startup schema path (create_all + apply_migrations under
the advisory lock) twice against the configured database, then asserts the
columns that only the migration layer adds actually exist. Exits 1 on any
failure — a broken migration must block the build, not surface at deploy.
"""
import asyncio
import sys

from sqlalchemy import text

from brain.database.migrations import apply_migrations
from brain.database.session import async_engine, init_db

EXPECTED_MIGRATED_COLUMNS = [
    ("decisions", "repo_path"),
    ("rules", "repo_path"),
    ("embeddings", "provider"),
    ("embeddings", "content_hash"),
    ("audit_events", "request_id"),
    ("context_packs", "repository_id"),
    ("context_packs", "repo_commit"),
    ("context_packs", "repository_id"),
    ("context_packs", "repo_commit"),
]


async def main() -> int:
    await init_db()  # full startup path: create_all + apply_migrations
    # Re-applying the migration layer must be a no-op, not an error — fresh
    # deploys and rolling restarts both rely on this.
    async with async_engine.begin() as conn:
        await apply_migrations(conn)

    missing = []
    async with async_engine.connect() as conn:
        for table, column in EXPECTED_MIGRATED_COLUMNS:
            res = await conn.execute(
                text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = :t AND column_name = :c"
                ),
                {"t": table, "c": column},
            )
            if res.scalar() is None:
                missing.append(f"{table}.{column}")
    await async_engine.dispose()

    if missing:
        print(f"MISSING migrated columns: {missing}", file=sys.stderr)
        return 1
    print("migrations applied and idempotent; migrated columns verified")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
