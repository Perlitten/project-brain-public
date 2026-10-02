# Install — Brain 0.9.0-rc1 (single-user)

One instance per user, on a machine you control, with your own repositories and
your own provider credentials. Verified commands below were run end-to-end on a
clean Ubuntu box; measured timings are in
[`docs/launch-readiness/2026-10-02-single-user-journey-validation.md`](../../launch-readiness/2026-10-02-single-user-journey-validation.md).

## Prerequisites

| Requirement | Minimum | Verified |
|---|---|---|
| Python | 3.10+ | 3.12.13 |
| Docker Engine + Compose | any recent | 29.7.2 / v5.4.0 |
| `psql` + `pg_dump` on PATH | match server major (pg15) | via `postgresql-client` |
| RAM / disk for services | ~2 GB free | — |
| Provider credentials | none required (mock works) | — |

Optional, only when you want real LLM/embeddings: `OPENAI_API_KEY` /
`ANTHROPIC_API_KEY` / `GOOGLE_API_KEY` / `NVIDIA_API_KEY` (nvapi-…).

## Install

```bash
git clone https://github.com/Perlitten/project-brain-public.git project-brain && cd project-brain

python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"          # ~45s

cp .env.example .env                        # edit TARGET_REPO_PATH, provider keys
```

`.env.example` ships a working default API key
(`PROJECT_BRAIN_API_KEY=change-me-in-production`). API auth is **on** out of the
box — every API call needs header `X-API-Key: <value>`. Change it to a random
token before exposing the port anywhere.

## Services

```bash
docker compose up -d postgres redis neo4j   # healthy in ~16s
```

Postgres `pgvector/pgvector:pg15` on host :5433, Redis on :6379, Neo4j on
:7474/:7687. `n8n` is optional orchestration — skip it if unused.

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
curl -H "X-API-Key: change-me-in-production" \
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

10 tools including `search_code`, `prepare_task_context`, `record_decision`.

Remote HTTP variant (`apps.mcp_server.remote_server`) exists for agents on
other machines; see `docs/` — not covered by this guide.

## Verify

```bash
.venv/bin/pytest tests/ -q        # ~51s; hermetic to your .env since #96
.venv/bin/ruff check .
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `brain doctor` FAIL: postgres/redis/neo4j down | `docker compose up -d postgres redis neo4j` |
| `pg_dump not found` during backup | install `postgresql-client` matching the server major version |
| 401 on every API call | send `X-API-Key` matching `.env` `PROJECT_BRAIN_API_KEY` |
| 503 with "API key required" | `ENVIRONMENT=production` + no key — set one or run `local` |
| Sandbox reports `unverified`/`unshare` | `docker pull debian:bookworm-slim` |
| `/dashboard` returns 404 | the built-in dashboard was removed; run `apps/web` and set `BRAIN_WEB_URL` |
