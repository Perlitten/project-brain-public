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
    ("decisions", "superseded_by_id"),
    ("rules", "repo_path"),
    ("embeddings", "provider"),
    ("embeddings", "content_hash"),
    ("audit_events", "request_id"),
    ("context_packs", "repository_id"),
    ("context_packs", "repo_commit"),
    ("context_packs", "repository_id"),
    ("context_packs", "repo_commit"),
    ("memory_episodes", "duplicate_of_learning_id"),
    ("memory_episodes", "gate_reasons"),
    # Provenance: every memory object names its author and last editor; decisions and
    # rules also carry the supersede/version chain (brain/memory/provenance.py).
    ("decisions", "updated_by_principal_id"),
    ("decisions", "supersedes_id"),
    ("decisions", "version"),
    ("rules", "updated_by_principal_id"),
    ("rules", "supersedes_id"),
    ("rules", "superseded_by_id"),
    ("rules", "version"),
    ("memory_learnings", "author_principal_id"),
    ("memory_learnings", "updated_by_principal_id"),
    ("memory_episodes", "author_principal_id"),
    ("memory_episodes", "updated_by_principal_id"),
    ("memory_skills", "author_principal_id"),
    ("memory_skills", "updated_by_principal_id"),
    ("memory_episode_events", "episode_id"),
    ("memory_skill_outcomes", "task_id"),
    ("memory_skill_outcomes", "validation_id"),
    # What a pack delivered and who asked for it: the agent-value report reads
    # these, so a migration that silently stops adding them must fail the build.
    ("context_packs", "token_estimate"),
    ("context_packs", "rule_count"),
    ("context_packs", "consumer"),
    ("rule_usage_events", "kind"),
    ("team_task_bindings", "request_digest"),
    ("agent_runs", "initiator_credential_id"),
    ("team_invitations", "token_hash"),
    # Every credential resolution reads the restore fence (brain.team.restore_fence).
    ("team_access_fences", "cleared_at"),
    # A team never falls back to solo on its own (brain.team.authorization.team_state).
    ("teams", "dissolved_at"),
    # Governed memory: proposals wait here until an independent reviewer decides (brain/memory/proposals.py).
    ("memory_proposals", "base_version"),
    ("memory_proposals", "reviewed_by_principal_id"),
    # Membership, role and queued-job history for the team activity feed (brain.team.activity).
    ("team_activity_events", "audience"),
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
