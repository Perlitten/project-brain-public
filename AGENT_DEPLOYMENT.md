# Project Brain Agent Deployment Contract

This file is the deployment contract for an AI coding agent such as Claude,
Cursor, Codex, or another autonomous shell operator. Human-friendly background
lives in `DEPLOYMENT.md`; this file is stricter and should be followed literally.

## Mission

Deploy a clean Project Brain instance for a new owner on a new Ubuntu VPS.

Success means:

- Project Brain runs from `/opt/project-brain`.
- API answers at `https://$BRAIN_PUBLIC_HOST/health`.
- Postgres, Redis, Neo4j, API, and worker are healthy; the worker logs `Scheduler started`.
- Target repository is indexed and embeddings verify cleanly.
- API smoke passes (health, authenticated read, 401 without a key).
- MCP config can be generated for the owner's AI client.

Do not migrate old local data, reports, context packs, indexes, database volumes,
or secrets unless the human explicitly asks for a migration plan.

## Required Human Inputs

Before touching the server, collect these values from the human or from a secure
secret picker. Do not guess them.

```text
VPS_SSH_HOST=
VPS_SSH_USER=
PROJECT_BRAIN_GIT_REMOTE=
SERVER_IP_OR_DOMAIN=
BRAIN_PUBLIC_HOST=
CERTBOT_EMAIL=
NVIDIA_API_KEY=
TARGET_REPO_GIT_REMOTE=
TARGET_REPO_HOST_PATH=
INDEXED_PROJECTS_HOST_PATH=
```

Use nip.io if there is no DNS:

```text
BRAIN_PUBLIC_HOST=brain.<server-ip>.nip.io
```

## Hard Rules

- Do not print, paste, commit, summarize, or screenshot real secrets.
- Do not commit `.env`, `.secrets/`, `reports/`, or `context_packs/`. `.mcp.json`
  is tracked and must keep `${VAR}` references instead of a literal key.
- Do not use stale hostnames, usernames, IPs, or local paths from another owner.
- Do not expose Postgres, Redis, or Neo4j to the public internet.
- Do not add nginx Basic Auth to the API vhost; the API authenticates with `X-API-Key`.
- Do not claim production success until every gate below has passed.
- If a command fails, stop and diagnose. Do not continue by assumption.

## Agent Workspace Rules

Before deploy work:

```bash
git status --short
git diff --check
```

If the worktree has unrelated user changes, leave them alone. Only edit deploy
docs/scripts/configs required by this deployment.

Before final response:

```bash
git status --short
git diff --check
```

Report modified files and verification commands. Do not include secret values.

## Phase 0: Local Repository Audit

Run from the Project Brain repo before publishing or handing to another agent:

```bash
python -m compileall apps brain scripts
python -m brain.version
python -m pytest -q tests/test_web_router.py tests/test_jobs_api.py tests/test_worker_scheduler.py
```

Run the stale-project scan. It must print `DOCTOR OK`:

```bash
bash deploy/doctor.sh stale-scan
```

Run the secret-shape scan. Placeholder/test hits are acceptable only in
`.env.example` and tests:

```bash
rg -n "(nvapi-[A-Za-z0-9_-]{20,}|sk-proj-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9_-]{20,}|BRAIN_DASHBOARD_PASSWORD=\\S{8,}|PROJECT_BRAIN_WEBHOOK_TOKEN=\\S{8,}|NEO4J_PASSWORD=\\S{8,}|POSTGRES_PASSWORD=\\S{8,}|PROJECT_BRAIN_API_KEY=\\S{8,})" \
  README.md AGENT_DEPLOYMENT.md DEPLOYMENT.md deploy .env.example .mcp.json.example \
  docker-compose.prod.yml apps brain scripts tests pyproject.toml
```

## Phase 1: Server Bootstrap

Run on the VPS:

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git nginx apache2-utils certbot python3-certbot-nginx openssl

curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"
newgrp docker

docker version
docker compose version
```

Clone the repo:

```bash
sudo mkdir -p /opt/project-brain
sudo chown -R "$USER:$USER" /opt/project-brain
git clone "$PROJECT_BRAIN_GIT_REMOTE" /opt/project-brain
cd /opt/project-brain
```

## Phase 2: Configure `.env`

Create `.env`:

```bash
cp deploy/.env.prod.example .env
chmod 600 .env
```

Replace placeholders:

```bash
sed -i "s|^BRAIN_PUBLIC_HOST=.*|BRAIN_PUBLIC_HOST=${BRAIN_PUBLIC_HOST}|" .env
sed -i "s|^BRAIN_TARGET_REPO_DIR=.*|BRAIN_TARGET_REPO_DIR=${TARGET_REPO_HOST_PATH}|" .env
sed -i "s|^BRAIN_INDEXED_PROJECTS_DIR=.*|BRAIN_INDEXED_PROJECTS_DIR=${INDEXED_PROJECTS_HOST_PATH}|" .env
```

Set `NVIDIA_API_KEY` without printing it:

```bash
read -rsp "NVIDIA_API_KEY: " NVIDIA_API_KEY
echo
sed -i "s|^NVIDIA_API_KEY=.*|NVIDIA_API_KEY=${NVIDIA_API_KEY}|" .env
unset NVIDIA_API_KEY
```

Proactive insights are available in Project Brain 0.2.0. Leave the LLM stage off
unless the owner explicitly wants NVIDIA NIM self-diagnosis enabled:

```bash
sed -i "s|^PROACTIVE_INSIGHTS_ENABLED=.*|PROACTIVE_INSIGHTS_ENABLED=false|" .env
sed -i "s|^PROACTIVE_INSIGHTS_LLM_ENABLED=.*|PROACTIVE_INSIGHTS_LLM_ENABLED=false|" .env
sed -i "s|^LLM_TASK_INSIGHT_MODEL=.*|LLM_TASK_INSIGHT_MODEL=meta/llama-3.1-8b-instruct|" .env
```

Clone the target repository:

```bash
mkdir -p "$INDEXED_PROJECTS_HOST_PATH"
git clone "$TARGET_REPO_GIT_REMOTE" "$TARGET_REPO_HOST_PATH"
```

Run preflight:

```bash
bash deploy/doctor.sh preflight
```

Gate: preflight must print `DOCTOR OK`.

## Phase 3: Start Stack

```bash
bash deploy/server_up.sh
bash deploy/doctor.sh post-start
```

Gate:

- compose config passes
- `brain-api` local health passes
- containers are running or healthy

## Phase 4: nginx and TLS

Install vhosts:

```bash
set -a && . ./.env && set +a

sudo cp deploy/nginx/brain.conf /etc/nginx/sites-available/brain.conf
sudo sed -i "s/brain.example.com/${BRAIN_PUBLIC_HOST}/g" /etc/nginx/sites-available/brain.conf
sudo ln -sf /etc/nginx/sites-available/brain.conf /etc/nginx/sites-enabled/brain.conf

sudo htpasswd -c /etc/nginx/.htpasswd-brain brain
sudo nginx -t
sudo systemctl reload nginx
```

Issue TLS:

```bash
sudo certbot --nginx -d "$BRAIN_PUBLIC_HOST" --redirect --agree-tos -m "$CERTBOT_EMAIL"
```

Run public checks:

```bash
bash deploy/doctor.sh public
```

Gate: public check must print `DOCTOR OK`.

## Phase 5: Scheduler and post-merge webhook

No setup: the worker runs the scheduler. Point the reindex workflow at the API:

```bash
gh variable set PROJECT_BRAIN_WEBHOOK_URL \
  --body "https://${BRAIN_PUBLIC_HOST}/webhooks/git-merge"
```

Keep `PROJECT_BRAIN_WEBHOOK_TOKEN` in GitHub Actions secrets. Both values are
required and the workflow fails closed when either is absent.

## Phase 6: Index Repository

```bash
docker compose -f docker-compose.prod.yml exec api python -m apps.cli.main index --repo /repo
docker compose -f docker-compose.prod.yml exec api python -m apps.cli.main embeddings verify --repo /repo
```

If verify fails because embeddings are missing or stale:

```bash
docker compose -f docker-compose.prod.yml exec api python -m apps.cli.main embeddings backfill --repo /repo --json
docker compose -f docker-compose.prod.yml exec api python -m apps.cli.main embeddings verify --repo /repo
```

Gate: final verify exits `0`.

## Phase 7: Proactive Insight Smoke

Run one deterministic insight scan and verify the endpoint can read stored
insights. Do not print the API key.

```bash
set -a && . ./.env && set +a
curl -fsS -X POST "http://127.0.0.1:${BRAIN_API_PORT:-8010}/jobs/proactive-insights" \
  -H "X-API-Key: ${PROJECT_BRAIN_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{}'
curl -fsS "http://127.0.0.1:${BRAIN_API_PORT:-8010}/insights" \
  -H "X-API-Key: ${PROJECT_BRAIN_API_KEY}" | head -c 500
```

Gate: POST returns a job id and GET returns JSON with an `insights` key. If the
worker has not completed yet, poll `/jobs/<job_id>` before treating an empty
insight list as a failure.

## Phase 8: Smoke Tests

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

## Phase 9: MCP Client Handoff

The repository ships a tracked, secret-free `.mcp.json` declaring the
`project-brain` server, so Claude Code needs environment values rather than a new
file. Do not overwrite that file from `.mcp.json.example`: it would commit a live
key. Set on the client machine (or on the cloud environment for web sessions):

```bash
BRAIN_API_URL="https://brain.<server-ip>.nip.io"
BRAIN_API_KEY="<PROJECT_BRAIN_API_KEY from server .env>"
BRAIN_REPO="/repo"
```

Clients that keep their MCP config outside the repository, such as Cursor, still
build it from `.mcp.json.example`.

Do not paste the actual API key into chat. The owner should copy it through the
client's secure secret mechanism when available.

## Failure Playbook

DNS fails:

- Check `dig $BRAIN_PUBLIC_HOST` or `getent hosts $BRAIN_PUBLIC_HOST`.
- For nip.io, verify the host includes the correct public IP.
- Do not edit nginx or certbot until DNS resolves.

Docker compose config fails:

- Run `bash deploy/doctor.sh preflight`.
- Check `.env` for placeholders and missing required variables.
- Do not remove required compose environment guards.

API health fails:

- Run `docker compose -f docker-compose.prod.yml logs --tail 100 api`.
- Check Postgres, Redis, and Neo4j container health.
- Do not expose database ports to debug.

Scheduled jobs overdue (`/health` → `degraded`):

- Run `docker compose -f docker-compose.prod.yml logs --tail 100 worker | grep Scheduler`.
- Check `SCHEDULER_ENABLED` and `GET /scheduler/jobs` for `last_error`.

Raw nginx 401 appears on the API host:

- Remove Basic Auth from the Brain API vhost.
- Run `sudo nginx -t && sudo systemctl reload nginx`.

Certbot fails:

- Check DNS first.
- Check that nginx port 80 is reachable.
- Do not hardcode certificate paths manually unless certbot has already issued them.

Embedding verify fails:

- Run backfill once.
- Check `NVIDIA_API_KEY` presence and provider errors in worker/API logs.
- Do not claim indexing complete until verify exits `0`.

## Final Report Template

Use this shape in the final deployment report:

```text
Deployment status: PASS|FAIL
Project Brain version: 0.2.0
Server path: /opt/project-brain
Brain URL: https://...
Target repo mounted as: /repo

Checks:
- deploy/doctor.sh preflight: PASS|FAIL
- deploy/server_up.sh: PASS|FAIL
- deploy/doctor.sh post-start: PASS|FAIL
- nginx -t: PASS|FAIL
- certbot Brain host: PASS|FAIL
- deploy/doctor.sh public: PASS|FAIL
- scheduler firing (worker log + /scheduler/jobs): PASS|FAIL
- index /repo: PASS|FAIL
- embeddings verify /repo: PASS|FAIL
- proactive insight smoke: PASS|FAIL
- API smoke: PASS|FAIL

Notes:
- No secrets printed.
- No `.env`, `.secrets`, `reports`, or `context_packs` committed, and no literal
  key in the tracked `.mcp.json`.
- Any skipped check has a concrete reason.
```

## Copy-Paste Prompt For An Agent

```text
You are deploying Project Brain on a fresh Ubuntu VPS.
Read AGENT_DEPLOYMENT.md first and follow it literally.
Do not print or commit secrets.
Do not use stale paths, old IPs, or another owner's repo data.
Use DEPLOYMENT.md only as background; AGENT_DEPLOYMENT.md is the authority.
Run deploy/doctor.sh at every gate.
Stop on any failed gate and report the exact failing command and sanitized error.
Final answer must use the Final Report Template from AGENT_DEPLOYMENT.md.
```
