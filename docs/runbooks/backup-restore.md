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

## Restoring a team Brain

A snapshot cannot know what happened after it was taken. A member, role or
credential revoked later is live again in the restored rows. So when the dump
has team members, the restore script writes an **access fence** in the same
transaction as the data (`scripts/team_restore_fence.sql`) and prints
`Team access is FENCED`. A solo snapshot (no team members) is restored unfenced,
as before.

While the fence is active:

- every named credential, OAuth token and run credential is refused, OAuth
  cannot issue or refresh tokens, and invitations cannot be accepted;
- the shared `PROJECT_BRAIN_API_KEY` and keyless dev-open access are refused
  too, even if no live member is left;
- `/health` reports `"team_access": "pending_reconciliation"` and `brain doctor`
  fails its `team_access` check.

The `brain team` CLI on the host is the break-glass path: it talks to the
database directly and is not subject to the fence. Reopen access like this:

1. Review the restored authorization state:

   ```bash
   .venv/bin/brain team reconcile
   ```

   It prints the roster, projects, roles, repository bindings, open tasks, how
   many credentials would be revoked, and a `digest` of all of it.

2. Replay everything that changed after the snapshot, from your records of
   that period: `brain team member revoke --principal NAME` (optionally
   `--project`), `brain team member add ... --project P --role R`, and so on.
   Each change alters the digest, so run step 1 again until the review is
   what the team should look like now.

3. Attest the final review and clear the fence:

   ```bash
   .venv/bin/brain team reconcile --attest <digest>
   ```

   A stale digest (the state changed since you reviewed it) is refused. By
   default this also **revokes every restored credential**: API keys
   (including service and OAuth-linked ones), OAuth refresh tokens, run
   credentials and unaccepted invitations. A key or invitation that was revoked
   after the snapshot is therefore dead even if nobody remembered to replay it.
   Issue new invitations where needed.

4. Give each remaining member a new key, with the issuer they were verified
   with (`brain team member list` shows it):

   ```bash
   .venv/bin/brain team member add --principal NAME --issuer <same issuer> --mint-key
   ```

   OAuth clients sign in again. Recreate other service keys the same way you
   created them originally.

`--keep-credentials` on step 3 keeps restored credentials. Use it only when the
snapshot is known to be current, for example a backup taken with the API
stopped immediately before this restore; otherwise a post-snapshot revocation is
silently undone. A restore done by hand (plain `psql` instead of
`scripts/restore.sh`) writes no fence: do not do that for a team Brain.
