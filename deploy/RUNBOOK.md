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
  deploy/nginx/brain-n8n.conf
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
- `brain-worker`
- `brain-n8n`

Named volumes:

- `brain_postgres_data`
- `brain_redis_data`
- `brain_neo4j_data`
- `brain_neo4j_logs`
- `brain_n8n_data`

## Security Model

- Postgres, Redis, and Neo4j are internal Docker services with no host ports.
- API binds to `127.0.0.1:${BRAIN_API_PORT:-8010}`.
- n8n binds to `127.0.0.1:${BRAIN_N8N_PORT:-5680}`.
- nginx terminates TLS for both public hosts.
- The API serves JSON only and authenticates every call with `X-API-Key`.
  `/dashboard*` only redirects to `BRAIN_WEB_URL`, the separately deployed
  web UI (`apps/web/`), which calls the API server-side and can be gated with
  `WEB_BASIC_AUTH`.
- n8n editor is protected by nginx Basic Auth.
- n8n `/webhook/` and `/webhook-test/` stay public at nginx level; workflows
  must authenticate calls into Brain with `PROJECT_BRAIN_API_KEY` or their own
  trigger secret.
- `deploy/server_up.sh` generates blank secrets in `.env`.
- `NVIDIA_API_KEY` is required in production and must be supplied by the deployer.
- `N8N_API_KEY` is optional. It only enables live n8n workflow introspection in
  the web UI.
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
curl -fsS http://127.0.0.1:${BRAIN_N8N_PORT:-5680}/healthz
```

Read public hosts from `.env`:

```bash
set -a && . ./.env && set +a
echo "Brain: https://${BRAIN_PUBLIC_HOST}/health"
echo "n8n:   https://${N8N_HOST}/"
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
| n8n watchdog | `deploy/brain_n8n_watchdog.sh` | medium | n8n container down or `/healthz` failing — restart per §Restart. |

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
docker compose -f docker-compose.prod.yml logs -f n8n
docker compose -f docker-compose.prod.yml logs -f postgres
docker compose -f docker-compose.prod.yml logs -f neo4j
```

## Restart

```bash
cd /opt/project-brain
docker compose -f docker-compose.prod.yml restart api worker
docker compose -f docker-compose.prod.yml restart n8n
bash deploy/server_up.sh
```

## nginx and TLS

Install or refresh vhosts after changing `BRAIN_PUBLIC_HOST` or `N8N_HOST`:

```bash
cd /opt/project-brain
set -a && . ./.env && set +a

# The conf references deploy/nginx/errors/brain_unavailable.html by absolute
# path — it must exist under the deployed checkout, so run this from
# /opt/project-brain (or adjust the alias path in the conf).
sudo cp deploy/nginx/brain.conf /etc/nginx/sites-available/brain.conf
sudo sed -i "s/brain.example.com/${BRAIN_PUBLIC_HOST}/g" /etc/nginx/sites-available/brain.conf
sudo ln -sf /etc/nginx/sites-available/brain.conf /etc/nginx/sites-enabled/brain.conf

sudo cp deploy/nginx/brain-n8n.conf /etc/nginx/sites-available/brain-n8n.conf
sudo sed -i "s/n8n.brain.example.com/${N8N_HOST}/g" /etc/nginx/sites-available/brain-n8n.conf
sudo ln -sf /etc/nginx/sites-available/brain-n8n.conf /etc/nginx/sites-enabled/brain-n8n.conf

sudo nginx -t
sudo systemctl reload nginx
```

Issue or renew certificates:

```bash
sudo certbot --nginx -d "$BRAIN_PUBLIC_HOST" --redirect
sudo certbot --nginx -d "$N8N_HOST" --redirect
```

n8n editor Basic Auth:

```bash
sudo htpasswd -c /etc/nginx/.htpasswd-brain brain
sudo nginx -t
sudo systemctl reload nginx
```

## Smoke Tests

```bash
set -a && . ./.env && set +a
curl -fsS "https://${BRAIN_PUBLIC_HOST}/health"
curl -fsS -H "X-API-Key: ${PROJECT_BRAIN_API_KEY}" \
  "https://${BRAIN_PUBLIC_HOST}/api/web/overview" > /dev/null && echo overview-ok
curl -s -o /dev/null -w '%{http_code}\n' "https://${BRAIN_PUBLIC_HOST}/api/web/overview"
```

Expected:

- `/health` returns JSON with `status`
- `/api/web/overview` with the key prints `overview-ok`
- `/api/web/overview` without the key returns `401`

The API serves JSON only. The human UI is the separate Next.js app in
`apps/web/` (see the README, "Web UI"); it has its own deploy and checks.

## Import n8n Workflows

After n8n first-run setup is complete:

```bash
cd /opt/project-brain
set -a && . ./.env && set +a
python3 scripts/import_n8n_workflows.py
```

Workflow exports live in `n8n/workflows/`.

## n8n Watchdog

Docker healthchecks mark unhealthy containers but do not restart them by
themselves. Install the watchdog timer:

```bash
sudo cp /opt/project-brain/deploy/systemd/brain-n8n-watchdog.service /etc/systemd/system/
sudo cp /opt/project-brain/deploy/systemd/brain-n8n-watchdog.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now brain-n8n-watchdog.timer
systemctl list-timers brain-n8n-watchdog.timer
```

If Project Brain is deployed outside `/opt/project-brain`, edit
`deploy/systemd/brain-n8n-watchdog.service` before copying it.

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

The `nightly-proactive-insights.json` workflow can enqueue the same job on a
schedule after n8n workflows are imported and activated. LLM synthesis is
controlled by `PROACTIVE_INSIGHTS_LLM_ENABLED`; deterministic checks run without
the LLM stage.

## Backups

Back up:

- `.env`
- Docker named volumes for Postgres, Redis, Neo4j, and n8n
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
- Never paste `PROJECT_BRAIN_API_KEY`, `WEB_BASIC_AUTH`, n8n Basic Auth
  password, `NVIDIA_API_KEY`, or `N8N_ENCRYPTION_KEY` into chat, docs, issues,
  screenshots, or reports.
- The web UI keeps `BRAIN_API_KEY` server-side; it never reaches the browser.
- `.env` should be `0600` on the server.
