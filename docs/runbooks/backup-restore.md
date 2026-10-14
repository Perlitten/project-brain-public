# Backup & restore runbook

## Automated daily backups

Backups live outside the VM's ephemeral disk. On the reference deployment:

```bash
mkdir -p ~/workspace/backups
```

Crontab (runs daily at 03:00, keeps the 7 newest):

```cron
0 3 * * * cd "$HOME/project-brain" && .venv/bin/python scripts/backup_runner.py --config "$HOME/project-brain/.env" --output "$HOME/workspace/backups/brain_backup_$(date +\%Y\%m\%d_\%H\%M\%S)" >> "$HOME/workspace/backups/backup.log" 2>&1 && ls -dt "$HOME"/workspace/backups/brain_backup_* | tail -n +8 | xargs -r rm -rf
```

Each run writes `backup_manifest.json` with SHA-256 checksums and per-artifact
status. The run fails (non-zero exit) if any required artifact was not produced —
no placeholder files are ever written.

Use the actual Brain `.env` (or set `BRAIN_ENV_FILE`) for both commands. The
recovery tools fail if this file is missing or incomplete; they do not silently
back up a default localhost database. Conflicting ambient database settings
are rejected rather than overriding the file. Inspect the *effective runtime*
configuration and keep this file in sync before scheduling. The example above
is for **full** (PostgreSQL/Neo4j) mode,
not Lite SQLite. Keep backups off the host and test restoration periodically.
The installed `pg_dump` must be at least the PostgreSQL server's major version;
`psql` must also be installed. A mismatched exporter fails rather than writing a
placeholder. Run recovery with the installed Brain virtual environment.

## Restore

1. Stop the API and worker (no writes during restore).
2. Start the data services: `docker compose up -d postgres redis neo4j`.
3. Run:

```bash
./scripts/restore.sh /path/to/brain_backup_YYYYMMDD_HHMMSS \
  --config "$HOME/project-brain/.env" --confirm-database brain_db
```

Replace `brain_db` with the **exact** `POSTGRES_DB`/`DATABASE_URL` database
specified by your configuration. The script verifies the manifest and
checksums, requires that exact confirmation, then drops/recreates only that
configured PostgreSQL database and loads `postgres_dump.sql`. This is
destructive to its target; **never run it against production during a drill**.
Use a separate disposable PostgreSQL instance/configuration for a rehearsal.
The wrapper uses `.venv/bin/python`; set `BRAIN_PYTHON` to a different installed
Brain interpreter if needed. No database is modified if configuration,
confirmation, manifest, or checksum validation fails.

4. Neo4j is **not** restored from the JSONL export — element_ids are not stable
   across databases. The graph is derived from the indexed repository, so
   rebuild it:

```bash
.venv/bin/brain index --repo /path/to/your/repo --clean
```

5. Start the API/worker and run `brain doctor`. The Neo4j JSONL artifact is
verified but **not restored** by this script. Until reindexing finishes, graph
queries can be incomplete. Redis queue jobs and Lite SQLite state are **not**
included in these full-mode backups; stop all workers before backup/restore and
plan to requeue or cancel interrupted jobs explicitly.
