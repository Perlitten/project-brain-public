---
title: RUNBOOK
created: '2026-08-03'
updated: '2026-08-10'
status: active
tags:
- type/note
---
# Project Brain - Production Runbook

This runbook is intentionally server-neutral. For first-time installation, use
the complete guide in `DEPLOYMENT.md`. This file is the shorter day-2 operations
checklist for an already deployed self-hosted instance.

Default production layout (use the actual release root for the host):

```text
${PROJECT_BRAIN_RELEASE_ROOT:-/opt/project-brain}/
  docker-compose.prod.yml
  .env                         # secrets, chmod 600, never committed
  deploy/server_up.sh
  deploy/nginx/brain.conf
  scripts/verify_release.sh
  apps/web/                    # Next.js app, deployed separately on Vercel
```

The managed VPS provider currently uses a release root inside the deploy
account's home — `~/project-brain`, not `/opt/project-brain`. Its host name,
login and key are deliberately uncommitted; take them from your SSH config or
the private ops notes. `check_no_stale_terms` in `deploy/doctor.sh` fails the
build when that identity appears in a tracked file, so do not paste it back
into this runbook.

If SSH is refused there, re-check the user name before suspecting the network.
The login is neither `root` nor a generic service account, and a rejected
guess is a wrong user name rather than a blocked port — do not diagnose it as
a local firewall and abandon an otherwise healthy deploy.

That host receives immutable `git archive` releases and records the exact SHA
in `.project-brain-release`; there is intentionally no `.git` directory on it.
Build/start it with `bash deploy/server_up.sh` so image labels and
`/api/version` keep the same release SHA and source digest.

Containers in compose project `brain`:

- `brain-postgres`
- `brain-redis`
- `brain-neo4j`
- `brain-api`
- `brain-worker` (also runs the scheduler)

Named volumes:

- `brain_postgres_data`
- `brain_redis_data`
- `brain_neo4j_data`
- `brain_neo4j_logs`

## Security Model

- Postgres, Redis, and Neo4j are internal Docker services with no host ports.
- API binds to `127.0.0.1:${BRAIN_API_PORT:-8010}`.
- nginx terminates TLS for the public API host.
- The Next.js dashboard uses `WEB_BASIC_AUTH=user:password` on Vercel.
  Live `BRAIN_API_URL` + `BRAIN_API_KEY` credentials require this gate; missing
  or malformed gate configuration returns 503 instead of exposing live data.
- The self-hosted API uses API-key and scope dependencies. `/dashboard/*`
  redirects to `BRAIN_WEB_URL`; there is no legacy login form or session cookie.
- `POST /webhooks/git-merge` is the only unauthenticated-by-API-key route; it
  requires `X-Project-Brain-Token` = `PROJECT_BRAIN_WEBHOOK_TOKEN` (constant-time
  compare) and fails closed (503) when the token is unset.
- `deploy/server_up.sh` generates blank secrets in `.env`.
- `NVIDIA_API_KEY` is required in production and must be supplied by the deployer.
- Change-lab validation commands run inside an OS sandbox (`LAB_SANDBOX_MODE`,
  default `auto`): a disposable container when the Docker daemon and
  `LAB_SANDBOX_IMAGE` are present (requires `/var/run/docker.sock` in the
  worker/api container), else unprivileged user+net+PID+mount namespaces via
  `unshare` when the kernel permits them. Validation reports record
  `enforced:docker` / `enforced:unshare`; `unverified` means no backend was
  available and isolation is policy-only — treat that as a launch blocker for
  hostile-patch evaluation. Inside Docker containers, `unshare` may be blocked
  by seccomp; prefer the docker backend there.

## Start or Update

For a checkout-based installation:

```bash
cd /opt/project-brain
git pull --ff-only
bash deploy/server_up.sh
```

The managed archive-based host has no `.git` directory. Build
the archive from a committed SHA on the trusted workstation, copy it to the
host, take a timestamped copy of the current release, and replace only source
files. Preserve `.env`, `reports/`, `context_packs/`, repository mounts and
Docker volumes. Write the committed SHA to `.project-brain-release`, then run
`bash deploy/server_up.sh`. Never synthesize a release SHA from an uncommitted
tree.

`server_up.sh` validates `.env`, generates missing secrets, creates host mount
directories, builds images, starts the stack, and waits for health checks.
Before any production build or container mutation it also queries
`BRAIN_GITHUB_CI_WORKFLOW` for the exact `.project-brain-release` SHA. A missing,
pending, cancelled or failed GitHub Actions run is a hard deploy failure; do not
override it by changing the release stamp. The production `.env` must set
`BRAIN_GITHUB_REPOSITORY=<owner>/<repo>` (the repository you deploy from) and `BRAIN_GITHUB_CI_WORKFLOW=ci.yml`.
Put the fine-grained `Actions: Read` `BRAIN_GITHUB_TOKEN` only in host-mode-600
`.deploy.env`; the file is intentionally excluded from Docker Compose runtime
environment.
Runtime settings and the deploy script refuse LFM experiment flags without
release/model/index-bound `LATE_INTERACTION_EXPERIMENT_AUTHORIZED=true`. They
separately refuse rerank/non-zero canary without
`LATE_INTERACTION_PRODUCTION_GATES_PASSED=true`. The running container reads
the build identity from its baked `.brain-source-manifest.json`, not from
mutable `.env` release fields. Production traffic additionally requires a
relative evidence path under `reports/`; both preflight and runtime open that
file and verify its SHA-256. Active approval is also bound to the sidecar's
persistent corpus lineage, identity digest, and verified document count. The
production eval corpus and evaluator are baked into the same source digest;
host bind mounts cannot replace them. Remote LFM registration alone is inert
and remains valid for offline sync/eval.

## Health Checks

```bash
cd /opt/project-brain
docker compose -f docker-compose.prod.yml ps
curl -fsS http://127.0.0.1:${BRAIN_API_PORT:-8010}/health
```

Read public hosts from `.env`:

```bash
set -a && . ./.env && set +a
echo "Brain: https://${BRAIN_PUBLIC_HOST}/health"
```

## Alerts and first response

Brain has three alert surfaces. Every alert dedupes by fingerprint for
`TELEGRAM_ALERT_COOLDOWN_SECONDS` (default 6h) so a persistent fault produces
one message, not a stream.

### What can fire

| Signal | Source | Severity | First response |
|---|---|---|---|
| `/ready` returns `not_ready` or no answer | `deploy/brain_watchdog.sh` → Telegram | high | Read `checks` in the payload — it names the failing dependency (below). `curl $API/ready` then check `docker compose ps` + `logs` for that service. |
| Repository freshness `stale`/`source_missing`/`behind` | `deploy/brain_watchdog.sh` → Telegram | medium | `/app` or `.` reporting `unverifiable` is the app indexing itself — expected on first boot. Otherwise re-index: `POST /jobs/reindex` for the listed repo. |
| Maintenance / deep-context / self-diagnosis insights | `brain/alerts/telegram.py` (`deliver_maintenance_alert`, `deliver_deep_context_alert`, `deliver_diagnosis_alert`) | low-medium | Insights are informational digests of failed checks found during scheduled jobs. Open the linked report; treat repeated identical fingerprints as an unresolved incident. |
| Scheduled job overdue (`/health` → `degraded`, `scheduler.stale`) | `brain/workers/scheduler.py` via `/health`; optional `DEADMAN_URL_*` pings | medium | Is a worker running with `SCHEDULER_ENABLED=true`? Read worker logs for `Scheduler fired`; run it now from Settings → Scheduler or `POST /jobs/*`. Final failures enqueue `self_diagnosis` (`trigger.source=job_failure`). |

### `/ready` check names → where to look

| Check | Failure means | Look at |
|---|---|---|
| `datastores` | postgres, redis, or neo4j unreachable | `docker compose logs postgres redis neo4j` |
| `release_identity` | production build missing `build_sha`/`source_digest` | release stamp step skipped — see §Rollback |
| `self_diagnosis` | `SELF_DIAGNOSIS_ENABLED` without Telegram alert config | set `TELEGRAM_ALERT_*` or disable `SELF_DIAGNOSIS_ENABLED` |
| `llm_provider` | production on nvidia provider without `NVIDIA_API_KEY` | configure the provider key or switch `DEFAULT_LLM_PROVIDER` |
| `worker_heartbeat` | no worker heartbeat within TTL | `docker compose logs worker`; worker crashed or Redis unreachable |
| `worker_capacity` | pools alive but all queues non-empty with `available_capacity: 0` | worker saturated or deadlocked — restart worker, inspect oldest job via `/jobs` |
| `late_interaction_release` | remote ColBERT sidecar not `ready` | `docker compose logs lfm-colbert`; retrieval continues on baseline (fail-open) |

### Autonomic incidents

`brain/operations/incidents.py` records root incidents with a fingerprint;
`brain/operations/remediation.py` maps each `incident_type` to a registered
`RepairRecipe` (`REINDEX_SOURCE_REVISION_MISMATCH`, `VECTOR_COVERAGE_GAP`,
`COMPARATOR_NAMESPACE_DEFECT`, `RETRIEVAL_FLOOD_CONTAINED`). An incident with no
recipe is rejected with `No pre-registered RepairRecipe found` — investigate
manually, do not force remediation.

## Resource budgets

Default limits protecting a self-hosted deployment. All are settings-backed;
raise only after measuring the bottleneck.

| Budget | Setting | Default | Enforcement |
|---|---|---|---|
| API request rate | nginx `limit_req_zone brain_api_rl` | 10 r/s, burst 20 | 503 at the edge (deploy nginx only) |
| API body size | `API_MAX_REQUEST_BODY_BYTES` | 25 MiB | 413 — middleware (mirrors `client_max_body_size`; applies even when uvicorn is exposed without nginx) |
| Job queue depth | `WORKER_QUEUE_MAX_DEPTH` | 5000 per prefix | `QueueDepthExceeded` → 429 with `Retry-After`; counts queued+processing+retrying |
| Job timeout | `WORKER_JOB_TIMEOUT_SECONDS` / `WORKER_MAX_JOB_TIMEOUT_SECONDS` | 1h / 6h ceiling | worker kills and retries/fails the job |
| Postgres pool | `POSTGRES_POOL_SIZE` + `POSTGRES_MAX_OVERFLOW` | see settings | pool exhaustion waits, then errors |
| Lab sandbox | `LAB_SANDBOX_*` | docker/unshare, pids 256 | validation commands refused without isolation |
| Patch upload | `MAX_PATCH_BYTES` | router constant | 413 before write |

Rate limiting itself is *not* applied when uvicorn is exposed directly
(nginx-only); put the proxy in front or accept unthrottled requests.

## Logs

```bash
cd /opt/project-brain
docker compose -f docker-compose.prod.yml logs -f api
docker compose -f docker-compose.prod.yml logs -f worker
docker compose -f docker-compose.prod.yml logs -f postgres
docker compose -f docker-compose.prod.yml logs -f neo4j
```

## Restart

```bash
cd /opt/project-brain
docker compose -f docker-compose.prod.yml restart api worker
bash deploy/server_up.sh
```

## nginx and TLS

Install or refresh the vhost after changing `BRAIN_PUBLIC_HOST`:

```bash
cd /opt/project-brain
set -a && . ./.env && set +a

# The conf references deploy/nginx/errors/brain_unavailable.html by absolute
# path — it must exist under the deployed checkout, so run this from
# /opt/project-brain (or adjust the alias path in the conf).
sudo cp deploy/nginx/brain.conf /etc/nginx/sites-available/brain.conf
sudo sed -i "s/brain.example.com/${BRAIN_PUBLIC_HOST}/g" /etc/nginx/sites-available/brain.conf
sudo ln -sf /etc/nginx/sites-available/brain.conf /etc/nginx/sites-enabled/brain.conf

sudo nginx -t
sudo systemctl reload nginx
```

Issue or renew certificates:

```bash
sudo certbot --nginx -d "$BRAIN_PUBLIC_HOST" --redirect
```

## Smoke Tests

The API and Next.js UI deploy separately. Run local release gates with a
prepared Python 3.12 environment, configured datastores and Node 22:

```bash
bash scripts/verify_release.sh
```

The script runs Python lint, types, migrations, tests, deployment identity and
wheel checks, then the Next.js auth/data regressions, typecheck and build.
Skipped gates exit 2 (incomplete); failures exit 1. Actions being disabled
does not turn these local checks into a CI run.

For a deployed live UI, verify an unauthenticated request to `BRAIN_WEB_URL`
and `/api/brain/web/reports` returns 401. A 503 means live authentication is
missing or malformed. Enter the configured Basic credentials in the browser,
then verify Overview, Activity, Reports, Get Started and a second repository's
Indexing and Code Map. Check both desktop and mobile widths for overflow.
The UI has no mutation controls; repair with the CLI commands in DEPLOYMENT.md.

Check the self-hosted API's `/health` and `/api/version`, and confirm
`/dashboard/` redirects to the configured `BRAIN_WEB_URL`. The Vercel project
Root Directory must be `apps/web`. A successful deployment must match the
release commit; an older READY deployment is insufficient.

## Scheduler

Recurring jobs are fired by the worker itself (`brain/workers/scheduler.py`,
[ADR 013](../docs/adr/013-worker-scheduler.md)); there is no n8n and no watchdog.

| Job | Cron (UTC) | Pool |
|---|---|---|
| `nightly_maintenance` | `30 0 * * *` | deep |
| `health_check` | `0 2 * * *` | fast |
| `self_diagnosis` | `30 3 * * *` | fast |
| `benchmark` | `0 6 * * 1` | maintenance |

- Exactly once per slot: a Redis `SET NX` lock per job type + slot time, so any
  number of workers or restarts never double-run a slot.
- A slot missed while no worker ran fires once on the next tick if it is at most
  `SCHEDULER_CATCHUP_WINDOW_S` (default 6h) old.
- Retries: `SCHEDULER_JOB_MAX_ATTEMPTS` (default 3), backoff
  `WORKER_RETRY_BASE_DELAY_S`·2^(n-1) capped at `WORKER_RETRY_MAX_DELAY_S`.
  A final failure records `last_failure_at`/`last_error` and enqueues `self_diagnosis`.
- `/health` is `degraded` when a job's last success is older than its interval +
  `SCHEDULER_STALE_GRACE_S` (default 2h). `GET /scheduler/jobs` has the details.
- Optional dead-man switch: `DEADMAN_URL_<JOB_TYPE>` is GET-pinged after each success.
- `SCHEDULER_ENABLED=false` turns it off (manual `POST /jobs/*` keeps working).

Verify it is firing:

```bash
docker compose -f docker-compose.prod.yml logs worker | grep -E "Scheduler (started|fired)"
curl -fsS -H "X-API-Key: $PROJECT_BRAIN_API_KEY" http://127.0.0.1:${BRAIN_API_PORT:-8010}/scheduler/jobs
```

## Index a Repository

The target repository is mounted read-only at `/repo`. Additional repositories
can be mounted under `/indexed`.

```bash
cd /opt/project-brain
docker compose -f docker-compose.prod.yml exec api python -m apps.cli.main index --repo /repo
docker compose -f docker-compose.prod.yml exec api python -m apps.cli.main embeddings verify --repo /repo
```

Repair missing or stale embeddings:

```bash
docker compose -f docker-compose.prod.yml exec api python -m apps.cli.main embeddings backfill --repo /repo --json
docker compose -f docker-compose.prod.yml exec api python -m apps.cli.main embeddings verify --repo /repo
```

Optional file cards:

```bash
docker compose -f docker-compose.prod.yml exec api python -c \
  "import asyncio; from brain.indexers.file_indexer import FileIndexer; asyncio.run(FileIndexer().build_file_cards('/repo'))"
```

Only enable `RETRIEVAL_V6_FILE_CARDS_ENABLED=true` after file cards are built.

## Proactive Insights

Manual scan:

```bash
cd /opt/project-brain
set -a && . ./.env && set +a
curl -fsS -X POST "http://127.0.0.1:${BRAIN_API_PORT:-8010}/jobs/proactive-insights" \
  -H "X-API-Key: ${PROJECT_BRAIN_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{}'
```

The scheduler enqueues `self_diagnosis` nightly at 03:30 UTC. LLM synthesis is
controlled by `PROACTIVE_INSIGHTS_LLM_ENABLED`; deterministic checks run without
the LLM stage.

## Backups

Back up:

- `.env`
- Docker named volumes for Postgres, Redis, and Neo4j
- `reports/`
- `context_packs/`

Postgres dump:

```bash
cd /opt/project-brain
set -a && . ./.env && set +a
docker compose -f docker-compose.prod.yml exec postgres \
  pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" > /tmp/project-brain.sql
```

## Rollback

For a checkout-based installation:

```bash
cd /opt/project-brain
git log --oneline -5
git checkout <known-good-commit>
bash deploy/server_up.sh
```

On the managed archive-based host, select a verified timestamped directory
under the deploy account's `~/project-brain-backups/`, confirm its
`.project-brain-release`, and restore its source tree while preserving the live
`.env`, `reports/`, `context_packs/`, repository mounts and Docker named
volumes. Then run `bash deploy/server_up.sh` and verify `/api/version` matches
the restored `.project-brain-release`. A host-side `git checkout` is not a
valid rollback there because the release root intentionally contains no
`.git`.

`docker compose -f docker-compose.prod.yml down` stops the stack but keeps named
volumes. Remove volumes only after taking backups.

### Release image retention

The rollback window above is made of the `brain-api:<sha>` images left on the
host by earlier deploys, so they are not garbage. After the health gates pass,
`server_up.sh` keeps the `BRAIN_IMAGE_RETENTION` (default 5) most recent ones
plus every tag a container still references, and removes only older `brain-api`
tags — nothing from another repository, and never during a deploy that failed.
Widen the window in `.env` before a risky release:

```bash
BRAIN_IMAGE_RETENTION=10
```

Retention failures are logged as a warning and never fail the deploy. To see
what is currently retained:

```bash
docker images --filter reference='brain-api:*'
```

## Secret Rules

- Never commit `.env`, `.secrets/`, `reports/`, or `context_packs/`.
- `.mcp.json` is tracked and must stay secret-free: `BRAIN_API_URL` and
  `BRAIN_API_KEY` are `${VAR}` references resolved from the environment. Never
  paste a literal key into it; `tests/test_mcp_project_config.py` fails the build.
- Never paste `PROJECT_BRAIN_API_KEY`, `PROJECT_BRAIN_WEBHOOK_TOKEN`, `WEB_BASIC_AUTH`,
  or `NVIDIA_API_KEY` into chat, docs, issues,
  screenshots, or reports.
- The web UI is read-only. Its server attaches the API key; browser bundles
  must never contain `BRAIN_API_KEY` or `PROJECT_BRAIN_API_KEY`.
- `.env` should be `0600` on the server.
