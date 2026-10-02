# Support matrix — Brain 0.9.0-rc1

"Supported" = the combination the release was actually verified on. Anything
outside it may work but is untested — treat as best-effort.

## Runtime

| Axis | Supported | Notes |
|---|---|---|
| OS | Linux x86_64 | verified on Ubuntu. macOS untested; Windows dev instructions exist in README but unverified |
| Python | 3.10, 3.11, 3.12 | verified on 3.12.13; `target-version = py310` |
| Install mode | `pip install -e ".[dev]"` (source) | wheel/sdist build verified by #69 smoke; wheel install not re-run for rc1 |
| Deploy | Docker Compose services + host venv | full compose stack (api/worker images) exists but the verified profile runs API/worker on host |

## Required services

| Service | Supported version | Verified image |
|---|---|---|
| PostgreSQL + pgvector | 15 | `pgvector/pgvector:pg15` |
| Redis | 7 | `redis:7-alpine` |
| Neo4j | 5.x | `neo4j:5.12-community` |

pg client tools on the operator host must match the server major version —
`pg_dump` 14 vs server 15 fails.

## Optional services

| Service | Status |
|---|---|
| n8n (orchestration) | optional; backup tolerates its absence (`--allow-missing`) |
| Remote embedding/late-interaction provider | optional; off by default |
| Sandbox image `debian:bookworm-slim` | required only for docker sandbox backend |

## LLM / embedding providers

| Provider | Status |
|---|---|
| `mock` | default, fully supported, verified |
| openai / anthropic / google / nvidia | supported in config; **not verified with live keys for rc1** |

## Agent integration

| Surface | Status |
|---|---|
| MCP stdio (`apps.mcp_server.server`) | verified — initialize + 10 tools + search |
| MCP remote HTTP (`apps.mcp_server.remote_server`) | exists; unverified for rc1 |
| HTTP API (`/search`, `/context`, …) | verified; `X-API-Key` required by default |

## Explicitly out of scope for this release

Organization management, SSO/SAML/SCIM, multi-tenant isolation, enterprise
roles, billing — deferred per product scope (single-user distribution).
Existing authentication, scoped credentials, and audit are kept.
