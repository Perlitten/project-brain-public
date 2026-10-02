# Upgrading & rollback — Brain 0.9.0-rc1

## How schema changes apply

There is no external migration tool (no alembic). `init_db()` at API/worker
startup runs `create_all` + `apply_migrations` under a Postgres advisory lock —
idempotent by design. `scripts/check_migrations.py` proves it: fresh init +
re-apply must be a no-op and exits 1 otherwise.

## Upgrade procedure

The example uses the default Compose service credentials and no n8n. Replace
connection values with your configured credentials. For an n8n installation,
omit `--allow-missing n8n` and supply `--n8n-source` with its SQLite file.
The backup script reads process environment variables; it does not load `.env`.

```bash
# 1. Back up first — rollback depends on it
PG_BACKUP_DSN='postgresql://postgres:postgres_password@localhost:5433/brain_db'
NEO4J_URI=bolt://localhost:7687 NEO4J_USER=neo4j NEO4J_PASSWORD=neo4j_password \
    .venv/bin/python scripts/backup_runner.py --output ./backups/$(date +%F) \
    --pg-dsn "$PG_BACKUP_DSN" --allow-missing n8n
.venv/bin/python scripts/restore_verification_drill.py \
    ./backups/$(date +%F) --pg-url "$PG_BACKUP_DSN"

# 2. Pull and reinstall
git pull --ff-only
.venv/bin/pip install -e ".[dev]"

# 3. Verify migrations idempotent against your live DB
.venv/bin/python scripts/check_migrations.py

# 4. Restart services, then re-index incrementally
docker compose restart              # or restart your host processes
.venv/bin/brain index --repo /path/to/your/repo  # unchanged files skip; ~4s on a 714-file repo
```

State survives restart: index, embeddings, jobs, audit — verified in the
journey report (step 9–10).

## Rollback

There are **no down-migrations**. Rolling back means restoring data to a
pre-upgrade backup, then running the previous code.

```bash
# 1. Restore from the backup taken before upgrade
.venv/bin/python scripts/restore_verification_drill.py ./backups/<date> --pg-url <db-url>  # verify first
#    then apply the dump to the live database per scripts/restore documentation

# 2. Check out the previous release SHA and reinstall
git checkout <previous-sha>
.venv/bin/pip install -e ".[dev]"
```

Compatibility note: migrations are additive (columns/tables only). Running
older code against a newer schema is safe for additive columns; it is *not*
safe if the old code reads a shape the new migration renamed/dropped — none in
rc1, but verify against the release diff before skipping the restore.

## Failure modes and what to do

| Situation | Recovery |
|---|---|
| Worker killed mid-job | automatic — lease expires (~60s+), `recover_stale` requeues, next worker retries (max_attempts 3). Verified live. |
| Migration fails at startup | startup aborts on error; advisory lock is released; fix DB state and restart — partial migrations do not strand the lock |
| Corrupt restore | drill reports `failed`/`artifacts_validated` with per-check detail; scratch DB is dropped automatically |
| `check_migrations.py` exits 1 | do not deploy; the listed column is missing — inspect `apply_migrations` log lines |
