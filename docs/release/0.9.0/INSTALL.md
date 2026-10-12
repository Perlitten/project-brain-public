# Install — Brain 0.9.0 (single-user)

One instance per user, on a machine you control, with your own repositories and
your own provider credentials. Verified commands below were run end-to-end on a
clean Ubuntu box.

**Windows:** `scripts/bootstrap.sh` is a POSIX shell script. On Windows run it
inside WSL2, or follow the manual steps below (use `.venv\Scripts\` instead of
`.venv/bin/`). Native Windows is not covered by CI.

## Prerequisites

| Requirement | Minimum | Verified |
|---|---|---|
| Python | 3.12 (CI-tested); `pyproject.toml` declares `>=3.10`, but 3.10/3.11 are untested | 3.12.13 |
| Docker Engine + Compose | Compose 2.24.0+ | 29.7.2 / v5.4.0 |
| RAM / disk for services | ~2 GB free | — |
| Provider credentials | none required (mock works) | — |

Optional, only when you want real LLM/embeddings: `OPENAI_API_KEY` /
`ANTHROPIC_API_KEY` / `GOOGLE_API_KEY` / `NVIDIA_API_KEY` (nvapi-…).

Optional, only for backups: `pg_dump` (used by `scripts/backup_runner.py`) and
`psql` (used by the live-restore leg of `scripts/restore_verification_drill.py`)
on PATH, from `postgresql-client` matching the server major version (pg15).
Nothing else needs them.

Optional, only for the web UI (`apps/web`): Node.js 22 (the version CI uses).

## Install

```bash
git clone https://github.com/Perlitten/project-brain.git project-brain && cd project-brain

python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"          # ~45s

cp .env.example .env                        # edit TARGET_REPO_PATH, provider keys
```

`scripts/bootstrap.sh` does all of the above and replaces the `.env.example`
placeholders for `PROJECT_BRAIN_API_KEY` and `PROJECT_BRAIN_WEBHOOK_TOKEN` with
random values. If you copy `.env.example` by hand, the placeholder API key
(`change-me-in-production`) still works locally, but replace both values with
random tokens (`python -c "import secrets; print(secrets.token_hex(32))"`):
with `ENVIRONMENT=production` the API refuses to start while either placeholder
is set. API auth is **on** out of the box — every API call needs the header
`X-API-Key: <value>`.

**`TARGET_REPO_PATH` vs `REPO_ROOT`.** `TARGET_REPO_PATH` is the repository the
server side works on by default — the API, worker jobs, web UI, `brain
embeddings` and `brain doctor` use it when no repository is given. `REPO_ROOT`
is only the default for the `--repo` option of `brain index`, `audit`,
`context`, `eval` and `review-diff` (default `.`, the current directory); pass
`--repo` explicitly to avoid surprises. Both paths are always accepted as
allowed repository roots.

## Services

```bash
docker compose up -d postgres redis neo4j   # healthy in ~16s
```

Postgres `pgvector/pgvector:pg15` on host :5433, Redis on :6379, Neo4j on
:7474/:7687, all bound to `127.0.0.1`. No separate scheduler service is needed:
scheduled jobs run inside the worker process (`brain/workers/scheduler.py`,
ADR 013); n8n is no longer used.

For the change-lab sandbox (optional): `docker pull debian:bookworm-slim`.
Without it `LAB_SANDBOX_MODE=auto` degrades to `unshare`/`off` — safe, but
validation commands then run with weaker/no OS containment.

## Preflight

After the services are running:

```bash
.venv/bin/brain doctor    # python, .env, dirs, provider keys, service health
```

Exits non-zero on FAILs, prints an actionable fix line per finding.

## Index + run

```bash
.venv/bin/brain index --repo /path/to/your/repo           # ~18s for 550 files
.venv/bin/uvicorn apps.api.main:app --port 8000          # /health reports postgres/redis/neo4j
```

First useful task (~50s from `docker compose up` on a warm box):

```bash
curl -H "X-API-Key: $(grep '^PROJECT_BRAIN_API_KEY=' .env | cut -d= -f2)" \
     -H "Content-Type: application/json" \
     -d '{"repo_path": "/path/to/your/repo", "task_description": "add oauth login", "persist": true}' \
     http://127.0.0.1:8000/context
```

## Connect an agent (MCP)

Local stdio server — direct DB access, no HTTP hop:

```bash
.venv/bin/python -m apps.mcp_server.server
```

Claude Code / Cursor config:

```json
{ "mcpServers": { "brain": {
  "command": "/path/to/project-brain/.venv/bin/python",
  "args": ["-m", "apps.mcp_server.server"],
  "cwd": "/path/to/project-brain"
} } }
```

13 tools (14 on the remote server) including `search_code`, `prepare_task_context`,
`record_decision`; the full list is in the [README](../../../README.md#9-mcp-server).

Remote HTTP variant (`apps.mcp_server.remote_server`) exists for agents on
other machines; see [`DEPLOYMENT.md`](../../../DEPLOYMENT.md) — not covered by this guide.

## Verify

```bash
.venv/bin/pytest tests/ -q        # hermetic to your .env
.venv/bin/ruff check .
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `brain doctor` FAIL: postgres/redis/neo4j down | `docker compose up -d postgres redis neo4j` |
| `pg_dump not found` during backup | install `postgresql-client` matching the server major version (backups only) |
| API fails at startup listing insecure defaults (`ENVIRONMENT=production`) | replace the `.env.example` placeholders with random values |
| 401 on every API call | send `X-API-Key` matching `.env` `PROJECT_BRAIN_API_KEY` |
| 503 with "API key required" | `ENVIRONMENT=production` + no key — set one or run `local` |
| Sandbox reports `unverified`/`unshare` | `docker pull debian:bookworm-slim` |
| `/dashboard` returns 404 | the built-in dashboard was removed; run `apps/web` and set `BRAIN_WEB_URL` |
