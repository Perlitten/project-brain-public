# Solo deploy checklist: c13d056 → main

Production runs `c13d056` (2026-10-13). This release brings in about 230
commits, including team mode. The goal is a deploy that changes **nothing** for
the owner: the Brain stays solo (no team member), and behaviour changes that
switched on by default are pinned back. A real team member comes later, as a
separate step.

**Gate: nobody runs anything below until the owner has given an explicit "yes"
for this SHA.** Everything up to the gate is read-only.

Placeholders: `<release-root>` is the deployed tree (the archive-based host has
no `.git`), `<new-sha>` is the main commit being deployed (CI must be green for
it), and `<old-sha>` is `c13d056`. Every `docker` command below names its target
explicitly. The host also runs the containers of another stack. Never run
`docker compose down`, `docker system prune`, `docker container prune`,
`docker volume prune` or any name pattern that could match them.

## 1. Pre-checks (read-only)

- [ ] GitHub Actions `ci.yml` is green for `<new-sha>` on
  Perlitten/project-brain-public. `server_up.sh` refuses otherwise.
- [ ] Time window. Do not deploy between 00:00 and 01:30 UTC, when nightly
  maintenance runs, or around the backup cron at 03:40 UTC. The deploy
  restarts the worker, and an in-flight job is abandoned. 08:00–22:00 UTC
  is safe.
- [ ] Record the other stack's containers, so you can show later that they are
  untouched:

  ```bash
  docker ps --format '{{.ID}} {{.Names}} {{.Image}} {{.Status}}' | grep -v ' brain-' | sort > ~/deploy-<new-sha>-others-before.txt
  ```

- [ ] Note the current state: `cat <release-root>/.project-brain-release`
  (expect `c13d056…`), `curl -fsS -H "X-API-Key: $PROJECT_BRAIN_API_KEY"
  http://127.0.0.1:${BRAIN_API_PORT:-8010}/health`, and
  `docker images --filter reference='brain-api:*'` (the `brain-api:<old-sha>`
  image is your fast rollback).
- [ ] Back up `.env` and `.deploy.env`: `cp -p .env ~/env.bak.<old-sha>` and
  `cp -p .deploy.env ~/deploy.env.bak.<old-sha>`, both mode 600. Never print them.
- [ ] Decision migration preview. Startup migration
  `_ensure_decision_title_uniqueness` marks duplicate decisions `superseded`
  and drops their original status. Run the read-only preview and keep the
  output:

  ```bash
  cd <release-root> && set -a && . ./.env && set +a
  docker exec -i brain-postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 \
    -c 'SET default_transaction_read_only = on' -f - < deploy/sql/preview_decision_supersede.sql \
    > ~/deploy-<new-sha>-decision-preview.txt
  ```

  Copy `deploy/sql/preview_decision_supersede.sql` from the new release first,
  because the old tree does not have it. The first result is the count; the second
  lists each decision that will be superseded and the decision that wins. If
  the count is not 0, read the list: a superseded row loses its status, and
  only the dump below brings it back.
- [ ] Find the repository path that is actually indexed (it is used in the
  env pins below):

  ```bash
  curl -fsS -H "X-API-Key: $PROJECT_BRAIN_API_KEY" http://127.0.0.1:${BRAIN_API_PORT:-8010}/repositories
  ```

- [ ] Verified pre-deploy dump. `server_up.sh` takes its own dump, but a
  failure there only prints a warning, and it keeps only the last five:

  ```bash
  docker exec brain-postgres pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB" > ~/brain-predeploy-<old-sha>.dump
  docker exec -i brain-postgres pg_restore --list < ~/brain-predeploy-<old-sha>.dump | tail -3
  ls -lh ~/brain-predeploy-<old-sha>.dump
  ```

- [ ] Backup cron. `scripts/backup_runner.py` and `scripts/restore_database.py`
  now **require** `--config <path to the Brain .env>` (or `BRAIN_ENV_FILE`).
  They also refuse to run when ambient `DATABASE_URL`, `POSTGRES_*`,
  `NEO4J_*` or `BRAIN_STORAGE_MODE` differ from that file. Read
  `~/backups/run-backups.sh` (it is not in the repo) and check:
  - Does it call `scripts/backup_runner.py`? If it only runs `pg_dump` via
    `docker exec brain-postgres`, it is unaffected.
  - If it does: does it pass `--config`? Does it run inside a container? The
    `.env` file is not in the image (`.dockerignore`), and compose overrides
    `DATABASE_URL`, `POSTGRES_HOST` and `NEO4J_URI` inside it. Both lead to
    "Recovery configuration file is missing" or "Ambient database settings
    differ".
  - Does it check the exit code? A refused run exits 1 and writes no dump. A
    script that ignores this stops backing up silently, and the weekly offsite
    pull then copies an old file.
  - After the deploy, run it once by hand and confirm a fresh
    `~/backups/brain/brain-*.sql.gz` with a non-trivial size.
- [ ] Old files. `tar --overwrite` never deletes. These files were removed
  between `c13d056` and main and stay behind on the host:
  `.deploy.env.example .mcp.json.example AGENT_DEPLOYMENT.md CODE_OF_CONDUCT.md
  CONTRIBUTING.md DEPLOYMENT.md ROADMAP.md THIRD_PARTY_NOTICES.md
  docker-compose.prod.yml docker-compose.quickstart.yml`. In particular, the
  production compose file is now `deploy/docker-compose.prod.yml` (`server_up.sh`
  uses `--project-directory . -f deploy/docker-compose.prod.yml`). The project
  name `brain`, the container names, the volumes and the mounts are unchanged.
  Remove the stale root `docker-compose.prod.yml` after extraction so nobody
  starts the old definition by habit. Never delete the real `.deploy.env`.

## 2. Env pins for a conservative rollout

Add these to `<release-root>/.env` before running `server_up.sh`. Each one
keeps the `c13d056` behaviour where main changed a default:

```bash
# Main defaults these to TARGET_REPO_PATH ("." = /app in the container). Pin to
# the indexed path you found in the pre-checks (c13d056 used /app).
PROJECT_BRAIN_GIT_REPO_PATH=/app
NIGHTLY_DEEP_MAINTENANCE_REPO_PATH=/app
# Default flipped False -> True.
RETRIEVAL_V2_ENABLED=false
# Default flipped off -> on.
BRAIN_AGENT_CONTEXT_V2_MODE=off
# New setting. c13d056 always blocked code-change context on a stale index;
# the main default (warn_and_serve) serves it with a warning instead.
BRAIN_STALE_CONTEXT_POLICY=strict_block
# Keep the strict team flag off: the Brain stays solo until a verified member exists.
BRAIN_TEAM_MODE=false
# Widen the image rollback window for this release.
BRAIN_IMAGE_RETENTION=10
```

Changes that cannot be pinned, and that you should know about:

- **Nightly maintenance now does real work.** At `c13d056`, the 00:30 UTC
  `nightly_maintenance` job returned `skipped` unless
  `NIGHTLY_DEEP_MAINTENANCE_ENABLED=true`. On main it always:
  - heals the other stale repositories;
  - runs an incremental reindex of the pinned repository;
  - verifies embeddings, and backfills them only if verification fails;
  - runs memory consolidation if `MEMORY_CONSOLIDATION_ENABLED`;
  - sends a Telegram summary. The scheduler passes `notify=true`, so
    `NIGHTLY_DEEP_MAINTENANCE_NOTIFY` does not silence it.

  If the pinned repository is not indexed, the job fails permanently. That
  enqueues `self_diagnosis` and turns `/health` `degraded`. No flag turns off
  only this job; `SCHEDULER_ENABLED=false` stops every scheduled job. Watch
  the first run instead (post-checks). Redis keeps the scheduler's
  per-slot locks, so a slot that already fired before the deploy does not
  fire again.
- `INDEX_LLM_SUMMARIES_MODE` is new, default `lazy`: indexing no longer makes
  one LLM call per file. This is what makes a post-deploy reindex finish in
  minutes rather than over an hour. Set it to `inline` only to reproduce the
  old indexing exactly.
- `MCP_TOOL_CALLS_RETENTION_DAYS` is new, default 30. The daily `health_check`
  deletes older rows from `mcp_tool_calls`, a table that is new on prod.

Flip the pins back one at a time after the solo deploy has been stable for a
few days, and record each change.

## 3. Deploy: API first, then web

- [ ] **API.** Use the archive flow from `deploy/RUNBOOK.md`:
  `git archive` of `<new-sha>`, then copy it to the host. Extract with
  `--exclude='reports' --exclude='reports/*'`, and do not chain the next step
  on `tar`'s exit code. Then write the stamp: `echo <new-sha> > .project-brain-release`. Remove the stale root
  `docker-compose.prod.yml`, add the env pins, and run `bash deploy/server_up.sh`.
  `brain-api` shows `unhealthy` for several minutes while the dump runs.
  That is normal. Poll; do not react.
- [ ] **Web**, only after the API post-checks pass. Deploy the Vercel project
  from `apps/web` (`npx vercel deploy --prod`). This release adds no new
  required web env var. In solo state the dashboard keeps using its server
  key. Team sign-in only appears once a member exists, so the web key
  question belongs to the member step, not this one.

## 4. Post-checks

- [ ] `/api/version` reports `<new-sha>`.
- [ ] `/health` returns `"status": "ok"`, `"mode": "solo"`,
  `"team_access": "open"`.
- [ ] `docker exec brain-api python -m apps.cli.main team member list` shows no
  members.
- [ ] The owner's own path works unchanged: a `prepare_task_context` or
  `search_code` call with the owner key, and an `ask_project`.
- [ ] OAuth connector: reconnect one (for example claude.ai). Approval succeeds,
  and the issued token carries **no** `team:*` scope. A solo Brain grants the
  personal scopes, even to a client that asks for every advertised scope.
- [ ] The decision migration did what the preview predicted:

  ```sql
  SELECT count(*) FROM decisions WHERE superseded_by_id IS NOT NULL;
  ```

- [ ] The other stack is untouched:

  ```bash
  docker ps --format '{{.ID}} {{.Names}} {{.Image}} {{.Status}}' | grep -v ' brain-' | sort > ~/deploy-<new-sha>-others-after.txt
  diff <(cut -d' ' -f1-3 ~/deploy-<new-sha>-others-before.txt) <(cut -d' ' -f1-3 ~/deploy-<new-sha>-others-after.txt) && echo UNCHANGED
  ```

  The IDs, names and images must match, and the uptimes must have kept
  counting rather than reset.
- [ ] The dashboard on Vercel loads the overview, repositories and insights
  with no sign-in prompt.
- [ ] After the first 00:30 UTC run: `GET /scheduler/jobs` shows
  `nightly_maintenance` succeeded, a Telegram summary arrived, and `/health`
  is not `degraded`.
- [ ] Run the backup cron once by hand, or check the 03:40 run, and confirm a
  fresh dump.

## 5. Rollback

Startup migrations only move forward, so this release cannot be rolled back
by swapping the image alone. A rehearsal on a migrated copy showed two things:

- `c13d056` starts, but superseded decisions stay superseded.
- It refuses to start (`could not create unique index
  "uq_decisions_normalized_title"`) once main has stored two decisions whose
  repository paths differ only in case.

**Rollback = restore the pre-deploy dump plus the previous release tree**,
following "Rollback across a schema migration" in `deploy/RUNBOOK.md`:

1. Stop only `brain-api` and `brain-worker`.
2. Restore `~/brain-predeploy-<old-sha>.dump`.
3. Restore the `c13d056` tree and run `server_up.sh`.
4. Verify `/api/version`.

Anything written after the deploy is lost. Restore the web on Vercel to the
previous production deployment from its dashboard. The last step: repeat the
check that the other stack is untouched.
