# Backup & restore runbook

## Automated daily backups

Backups live outside the VM's ephemeral disk. On the reference deployment:

```bash
mkdir -p ~/workspace/backups
```

Crontab (runs daily at 03:00, keeps the 7 newest):

```cron
0 3 * * * cd /home/hatch/workspace/projects/brain && /home/hatch/workspace/brain-env.sh .venv/bin/python scripts/backup_runner.py --output /home/hatch/workspace/backups/brain_backup_$(date +\%Y\%m\%d_\%H\%M\%S) >> /home/hatch/workspace/backups/backup.log 2>&1 && ls -dt /home/hatch/workspace/backups/brain_backup_* | tail -n +8 | xargs -r rm -rf
```

Each run writes `backup_manifest.json` with SHA-256 checksums and per-artifact
status. The run fails (non-zero exit) if any required artifact was not produced —
no placeholder files are ever written.

## Restore

1. Stop the API and worker (no writes during restore).
2. Start the data services: `docker compose up -d postgres redis neo4j`.
3. Run:

```bash
./scripts/restore.sh /path/to/brain_backup_YYYYMMDD_HHMMSS
```

The script verifies the manifest and checksums, asks for `RESTORE` confirmation,
then drops/recreates `brain_db` and loads `postgres_dump.sql`.

4. Neo4j is **not** restored from the JSONL export — element_ids are not stable
   across databases. The graph is derived from the indexed repository, so
   rebuild it:

```bash
.venv/bin/brain index --repo /path/to/your/repo --clean
```

5. Start the API/worker and run `brain doctor`.
