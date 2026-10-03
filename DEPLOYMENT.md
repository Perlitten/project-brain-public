# Project Brain Deployment Guide

This guide is the git-safe path for deploying a fresh Project Brain instance on a
new VPS. It does not use any existing private keys, passwords, or server-specific
paths from the original deployment.

If an AI coding agent is doing the work, use `AGENT_DEPLOYMENT.md` as the
authoritative checklist and this file as background.

The production stack runs:

- FastAPI app (`brain-api`) on loopback port `8010`
- background worker (`brain-worker`)
- Postgres + pgvector, Redis, Neo4j on an internal Docker network
- n8n (`brain-n8n`) on loopback port `5680`
- nginx + TLS in front of the API and n8n editor

## 0. What Your Friend Needs

- Ubuntu 22.04/24.04 VPS with sudo
- 4+ vCPU, 8+ GiB RAM recommended
- Docker Engine + Docker Compose plugin
- nginx, certbot, apache2-utils
- an API key for the LLM/embedding provider. The production default is NVIDIA
  NIM (`NVIDIA_API_KEY=nvapi-...`); any OpenAI-compatible provider works, see
  "Any OpenAI-compatible provider" below
- a repository to index, cloned on the VPS or copied there
- either real DNS records or nip.io hostnames based on the server IP

For a server IP `203.0.113.10`, nip.io hostnames can be:

```text
brain.203.0.113.10.nip.io
n8n.brain.203.0.113.10.nip.io
```

## 1. Server Bootstrap

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

Clone the repo to the default production path:

```bash
sudo mkdir -p /opt/project-brain
sudo chown -R "$USER:$USER" /opt/project-brain
git clone <YOUR_GIT_REMOTE_URL> /opt/project-brain
cd /opt/project-brain
```

## 2. Configure `.env`

Create the production env file:

```bash
cp deploy/.env.prod.example .env
chmod 600 .env
```

Set hostnames and the NVIDIA key. Replace `203.0.113.10` with the VPS IP or use
your real domains:

```bash
SERVER_IP=203.0.113.10
sed -i "s/REPLACE_WITH_SERVER_IP/${SERVER_IP}/g" .env
read -rsp "NVIDIA_API_KEY: " NVIDIA_API_KEY
echo
sed -i "s|^NVIDIA_API_KEY=.*|NVIDIA_API_KEY=${NVIDIA_API_KEY}|" .env
```

### Any OpenAI-compatible provider

NVIDIA is just one preset. `DEFAULT_LLM_PROVIDER` / `DEFAULT_EMBEDDING_PROVIDER`
accept `openai`, `nvidia`, `openrouter`, `groq`, `together`, `deepseek`,
`mistral`, `ollama`, `lmstudio`, or `openai_compatible` (aliases `custom`,
`openai-compatible`) for any other endpoint (vLLM, LiteLLM, Fireworks, ...).
The universal settings `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`,
`SUMMARIZER_MODEL`, `EMBEDDING_BASE_URL`, `EMBEDDING_API_KEY`,
`EMBEDDING_MODEL`, `EMBEDDING_DIMENSION`, `EMBEDDING_MAX_INPUT_CHARS` and
`EMBEDDING_BATCH_SIZE` override the preset defaults (base URLs include `/v1`).

`deploy/server_up.sh` and `deploy/doctor.sh preflight` require the key of the
configured provider (preset key such as `GROQ_API_KEY`, or `LLM_API_KEY`); the
`nvapi-` prefix is enforced only for `nvidia`. `ollama`, `lmstudio` and
`openai_compatible` may be keyless but need a base URL and model.

```bash
# OpenRouter for chat, keep NVIDIA embeddings (no re-index needed)
DEFAULT_LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-...
LLM_MODEL=anthropic/claude-3.5-sonnet
DEFAULT_EMBEDDING_PROVIDER=nvidia

# Groq for chat
DEFAULT_LLM_PROVIDER=groq
GROQ_API_KEY=gsk_...

# Ollama on the host or as a compose service, no key
DEFAULT_LLM_PROVIDER=ollama
LLM_BASE_URL=http://ollama:11434/v1
LLM_MODEL=llama3.1
```

Also clear or replace the NVIDIA-specific `LLM_TASK_*_MODEL` values when you
switch the LLM provider. Production already stores 4096-dim NVIDIA vectors:
switching `DEFAULT_EMBEDDING_PROVIDER`, `EMBEDDING_MODEL` or
`EMBEDDING_DIMENSION` means a full re-index. `brain doctor` flags an unknown or
conflicting dimension before anything is written.

Choose the repository mount. If the target repo is `/opt/repos/my-app`:

```bash
mkdir -p /opt/repos
git clone <FRIEND_TARGET_REPO_URL> /opt/repos/my-app

sed -i "s|^BRAIN_TARGET_REPO_DIR=.*|BRAIN_TARGET_REPO_DIR=/opt/repos/my-app|" .env
sed -i "s|^BRAIN_INDEXED_PROJECTS_DIR=.*|BRAIN_INDEXED_PROJECTS_DIR=/opt/repos|" .env
```

Any additional source tree you want indexed must reach the containers through a
**mount**. Do not stage sources with `docker cp`: that writes into the
container's writable layer, so the next `up --build` deletes them while their
rows in Postgres survive — and a clean re-index clears the index *before* it
rebuilds, so the following run destroys the index instead of refreshing it.
`BRAIN_NOSTIA_DIR`, `BRAIN_NOSTIA_VAULT_DIR`, `BRAIN_NOSTIA_MEMORY_DIR` and
`BRAIN_NOSTIA_SITE_DIR` exist for exactly this reason. Mount such trees
individually rather than mounting a shared parent, so a home directory holding
backups or ssh keys is never exposed to a container.

Before any clean re-index, confirm the container can actually see the source:

```bash
docker exec brain-worker test -d /repos/<name> || echo "REFUSE: clean would wipe this index"
```

Leave these values blank; `deploy/server_up.sh` generates strong secrets:

```text
POSTGRES_PASSWORD=
NEO4J_PASSWORD=
PROJECT_BRAIN_API_KEY=
N8N_ENCRYPTION_KEY=
```

`N8N_API_KEY` is optional. It only enables live n8n workflow introspection in the
Brain web UI. Project Brain works without it and shows repo workflow
definitions instead.

### Release gate

With `ENVIRONMENT=production`, `deploy/server_up.sh` refuses to build unless the
GitHub Actions workflow `BRAIN_GITHUB_CI_WORKFLOW` (default `ci.yml`) has a
**green run for the exact commit** being deployed. Set it up once:

1. Deploy from a GitHub repository whose Actions run `ci.yml` on push (your fork
   works) and set `BRAIN_GITHUB_REPOSITORY=<owner>/<repo>` in `.env`.
2. Create a fine-grained token with read-only **Actions** access to that
   repository and put it in a host-only file that Docker never sees:

```bash
cp .deploy.env.example .deploy.env
chmod 600 .deploy.env
read -rsp "BRAIN_GITHUB_TOKEN: " T; echo
sed -i "s|^BRAIN_GITHUB_TOKEN=.*|BRAIN_GITHUB_TOKEN=${T}|" .deploy.env; unset T
```

Deploy only commits whose CI run has finished green.

## 3. Start the Docker Stack

Run:

```bash
cd /opt/project-brain
bash deploy/server_up.sh
```

Expected:

- `brain-postgres` healthy
- `brain-redis` healthy
- `brain-neo4j` healthy
- `brain-api` healthy
- `brain-worker` running
- `brain-n8n` healthy

Useful checks:

```bash
docker compose -f docker-compose.prod.yml ps
curl -fsS http://127.0.0.1:8010/health
curl -fsS http://127.0.0.1:5680/healthz
```

## 4. Configure nginx and TLS

Read hostnames from `.env`:

```bash
cd /opt/project-brain
set -a && . ./.env && set +a
echo "$BRAIN_PUBLIC_HOST"
echo "$N8N_HOST"
```

Install the Brain API vhost:

```bash
sudo cp deploy/nginx/brain.conf /etc/nginx/sites-available/brain.conf
sudo sed -i "s/brain.example.com/${BRAIN_PUBLIC_HOST}/g" /etc/nginx/sites-available/brain.conf
sudo ln -sf /etc/nginx/sites-available/brain.conf /etc/nginx/sites-enabled/brain.conf
```

Install the n8n editor vhost:

```bash
sudo cp deploy/nginx/brain-n8n.conf /etc/nginx/sites-available/brain-n8n.conf
sudo sed -i "s/n8n.brain.example.com/${N8N_HOST}/g" /etc/nginx/sites-available/brain-n8n.conf
sudo ln -sf /etc/nginx/sites-available/brain-n8n.conf /etc/nginx/sites-enabled/brain-n8n.conf
```

Protect the n8n editor with Basic Auth. This is separate from the Project Brain
API key:

```bash
sudo htpasswd -c /etc/nginx/.htpasswd-brain brain
```

Validate and reload nginx:

```bash
sudo nginx -t
sudo systemctl reload nginx
```

Issue TLS certificates:

```bash
sudo certbot --nginx -d "$BRAIN_PUBLIC_HOST" --redirect
sudo certbot --nginx -d "$N8N_HOST" --redirect
```

Open:

```text
https://<BRAIN_PUBLIC_HOST>/health
https://<N8N_HOST>/
```

The API host serves JSON only. The web UI is the separate Next.js app in
`apps/web/` (deployed on Vercel); set `BRAIN_WEB_URL` in `.env` to its URL so
`/` advertises it and `/dashboard*` redirects there. Add the UI origin to
`CORS_ALLOWED_ORIGINS`.

## 5. Import n8n Workflows

The repo contains workflow exports in `n8n/workflows/`:

- `git-merge-reindex.json`
- `nightly-health.json`
- `nightly-proactive-insights.json`
- `weekly-benchmark.json`
- `indexing-error-notify.json`

After the n8n editor opens over HTTPS, import them:

```bash
cd /opt/project-brain
set -a && . ./.env && set +a
python3 scripts/import_n8n_workflows.py
```

If n8n asks for first-run setup, complete the local owner setup in the editor,
then rerun the import script. Workflow HTTP nodes call the internal Brain API at
`http://api:8000` with `PROJECT_BRAIN_API_KEY` from the n8n container env.

## 6. Install n8n Watchdog

Docker healthchecks report unhealthy containers but do not restart them by
themselves. Install the watchdog timer:

```bash
sudo cp deploy/systemd/brain-n8n-watchdog.service /etc/systemd/system/
sudo cp deploy/systemd/brain-n8n-watchdog.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now brain-n8n-watchdog.timer
systemctl list-timers brain-n8n-watchdog.timer
```

If you deployed somewhere other than `/opt/project-brain`, edit
`deploy/systemd/brain-n8n-watchdog.service` before copying it.

## 7. Index the Target Repo

Run from the VPS:

```bash
cd /opt/project-brain
docker compose -f docker-compose.prod.yml exec api python -m apps.cli.main index --repo /repo
docker compose -f docker-compose.prod.yml exec api python -m apps.cli.main embeddings verify --repo /repo
```

If embeddings are missing/stale:

```bash
docker compose -f docker-compose.prod.yml exec api python -m apps.cli.main embeddings backfill --repo /repo --json
docker compose -f docker-compose.prod.yml exec api python -m apps.cli.main embeddings verify --repo /repo
```

The web UI (`apps/web/`) displays index history. Run the CLI commands above
to reindex or repair embeddings; the web UI currently has no repair actions.

Optional file cards:

```bash
docker compose -f docker-compose.prod.yml exec api python -c \
  "import asyncio; from brain.indexers.file_indexer import FileIndexer; asyncio.run(FileIndexer().build_file_cards('/repo'))"
```

Only enable `RETRIEVAL_V6_FILE_CARDS_ENABLED=true` after cards are built.

## 8. Proactive Insights

Project Brain 0.2.0 includes a proactive insight inbox. It stores evidence-bound
signals in the `insights` table and serves them via `GET /insights` (shown in the web UI).

Run a manual scan:

```bash
cd /opt/project-brain
set -a && . ./.env && set +a
curl -fsS -X POST "http://127.0.0.1:${BRAIN_API_PORT:-8010}/jobs/proactive-insights" \
  -H "X-API-Key: ${PROJECT_BRAIN_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{}'
```

List stored insights:

```bash
curl -fsS "http://127.0.0.1:${BRAIN_API_PORT:-8010}/insights" \
  -H "X-API-Key: ${PROJECT_BRAIN_API_KEY}"
```

By default, scans use deterministic checks only. To allow the low-latency NVIDIA
NIM route to synthesize extra evidence-bound observations, set:

```text
DEFAULT_LLM_PROVIDER=nvidia
LLM_TASK_INSIGHT_MODEL=meta/llama-3.1-8b-instruct
PROACTIVE_INSIGHTS_LLM_ENABLED=true
```

Keep `PROACTIVE_INSIGHTS_LLM_ENABLED=false` for first deploys if NIM quota,
latency, or model availability is not verified yet. The deterministic fallback
still runs and saves insights if the LLM stage is disabled or fails.

## 9. Smoke Tests

The API host serves JSON only; smoke it with `curl`:

```bash
cd /opt/project-brain
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

The web UI (`apps/web/`) has its own checks and deploy pipeline.

## 10. Connect Claude/Cursor/Codex via MCP

Claude Code needs no per-machine file: the repository ships a tracked `.mcp.json`
that declares the `project-brain` server and resolves its settings from the
environment. Do not overwrite it with `.mcp.json.example` — that would put a live
key into a tracked file. Instead export the values where the client runs:

```bash
export BRAIN_API_URL="https://brain.<server-ip>.nip.io"
export BRAIN_API_KEY="<PROJECT_BRAIN_API_KEY from server .env>"
export BRAIN_REPO="/repo"
```

For Claude Code on the web, set the same three as environment variables on the
cloud environment; a session otherwise starts with the server declared but
unconfigured. `BRAIN_MCP_PYTHON` overrides the interpreter when `python3` is not
the right one for that machine.

For Cursor, which reads its own config outside the repository, copy
`.mcp.json.example` and put the `mcpServers` block in:

```text
%USERPROFILE%/.cursor/mcp.json
```

For Claude Desktop/Code, use that client's MCP config location.

## 11. Routine Operations

```bash
cd /opt/project-brain
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs -f api
docker compose -f docker-compose.prod.yml logs -f worker
docker compose -f docker-compose.prod.yml restart api worker
bash deploy/server_up.sh
docker compose -f docker-compose.prod.yml down
```

`down` stops the stack but keeps named volumes. To wipe data, remove named volumes
explicitly only after taking backups.

## 12. Backups

At minimum, back up:

- `.env`
- Docker named volumes: Postgres, Redis, Neo4j, n8n
- `reports/`
- `context_packs/`

Example Postgres dump:

```bash
docker compose -f docker-compose.prod.yml exec postgres \
  pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" > /tmp/project-brain.sql
```

## 13. Safety Rules

- Never commit `.env`, `.secrets/`, `reports/`, or `context_packs/`. `.mcp.json`
  is tracked and must keep `${VAR}` references instead of a literal key.
- Never put `PROJECT_BRAIN_API_KEY`, n8n Basic Auth password,
  NVIDIA key, or n8n encryption key into docs/chat/issues.
- The API host serves JSON only and authenticates with `X-API-Key`; the web UI
  is the separate `apps/web/` app. `/dashboard*` only redirects to `BRAIN_WEB_URL`.
- n8n `/webhook/` and `/webhook-test/` are intentionally public at nginx level.
  The workflows must authenticate calls into Brain with `PROJECT_BRAIN_API_KEY`
  or their own trigger secret.
