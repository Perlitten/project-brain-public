# Support matrix — Brain 0.9.0

"Supported" = the combination the release was actually verified on. Anything
outside it may work but is untested — treat as best-effort.

## Runtime

| Axis | Supported | Notes |
|---|---|---|
| OS | Linux x86_64 | verified on Ubuntu; CI runs on `ubuntu-latest`. macOS untested. Windows: use WSL2 (`scripts/bootstrap.sh` is POSIX-only); native Windows steps exist in README but are unverified |
| Python | 3.12 | verified on 3.12.13 and the only version CI tests. `pyproject.toml` declares `requires-python = ">=3.10"` (ruff `target-version = py310`), so 3.10/3.11 may work but are untested |
| Install mode | `pip install -e ".[dev]"` (source) | CI also builds a wheel and runs a clean-install smoke |
| Web UI (`apps/web`, optional) | Node.js 22 | the only version CI tests |
| Deploy | Docker Compose services + host venv | full compose stack (api/worker images) exists but the verified profile runs API/worker on host |

## Required services

| Service | Supported version | Verified image |
|---|---|---|
| PostgreSQL + pgvector | 15 | `pgvector/pgvector:pg15` |
| Redis | 7 | `redis:7-alpine` |
| Neo4j | 5.x | `neo4j:5.12-community` |

Only for backups (`scripts/backup_runner.py`, `scripts/restore_verification_drill.py`):
pg client tools on the operator host must match the server major version —
`pg_dump` 14 vs server 15 fails.

## Optional services

| Service | Status |
|---|---|
| Remote embedding/late-interaction provider | optional; off by default |

n8n is not used: scheduled jobs run inside the worker (`brain/workers/scheduler.py`,
[ADR 013](../../adr/013-worker-scheduler.md)), and backups no longer include an n8n
artifact.
| Sandbox image `debian:bookworm-slim` | required only for docker sandbox backend |

## LLM / embedding providers

| Provider | Status |
|---|---|
| `mock` | default, fully supported, verified |
| openai / anthropic / google / nvidia | supported in config; **not verified with live keys for this release** |

## Agent integration

| Surface | Status |
|---|---|
| MCP stdio (`apps.mcp_server.server`) | verified — initialize + tool listing + search (13 tools) |
| MCP remote HTTP (`apps.mcp_server.remote_server`) | exists (14 tools); not part of the verified single-user profile |
| HTTP API (`/search`, `/context`, …) | verified; `X-API-Key` required by default |

## Explicitly out of scope for this release

Organization management, SSO/SAML/SCIM, multi-tenant isolation, enterprise
roles, billing — deferred per product scope (single-user distribution).
Existing authentication, scoped credentials, and audit are kept.
