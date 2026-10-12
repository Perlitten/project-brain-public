# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security

- Production startup (`ENVIRONMENT=production`) now also rejects the
  `.env.example` placeholders for `PROJECT_BRAIN_API_KEY` and
  `PROJECT_BRAIN_WEBHOOK_TOKEN` (the default database passwords were already
  rejected in 0.9.0).
- `scripts/bootstrap.sh` replaces both placeholders with random 64-character
  hex secrets; `deploy/server_up.sh` regenerates any `change-me*` value.
- The development `docker-compose.yml` binds Postgres, Redis, Neo4j and the API
  to `127.0.0.1` instead of all interfaces.
- CORS no longer allows credentials when the allowed origin is the wildcard `*`.
- Web UI login is throttled per client IP after repeated failures.
- Health check errors report only the exception type, not its message.
- `oracle_test` commands and git refs that start with `-` are rejected, so
  they cannot be read as command-line options.
- The web UI fails closed: a live deployment without a configured login, or
  with a half-configured one, shows nothing and does not proxy API calls.
  Local development can opt in to the old open mode with `WEB_OPEN_MODE=true`.
- The web UI is marked `noindex` outside demo mode (`robots.txt`,
  `X-Robots-Tag`).
- Provider base URLs that point at cloud-metadata or link-local hosts are
  rejected.
- The `ci` and `benchmark` GitHub Actions workflows run with
  `permissions: contents: read`.

### Changed

- Telegram alert messages are in English.
- The web proxy returns a JSON 503 when the Brain API is unreachable.
- Release docs moved from `docs/release/0.9.0-rc1/` to `docs/release/0.9.0/`;
  historical design docs, benchmark results and pre-release reports removed.

### Fixed

- The NVIDIA embedding provider no longer fails on code that contains a
  `data:image/...;base64,` literal. NIM parsed it as an image input and
  rejected the request, so those chunks never got embeddings.
- Blank chunks left over from empty files no longer count as missing
  embeddings, so nightly verification of repositories with an empty
  `__init__.py` stops reporting "degraded" (providers reject empty input).
- The `benchmark` workflow's service containers use the same database
  passwords as the application defaults, so the job can connect.

## [0.9.0] - 2026-10-12

First public release of the single-user distribution. Full notes:
[`docs/release/0.9.0/RELEASE_NOTES.md`](docs/release/0.9.0/RELEASE_NOTES.md).

### Added

- Attributable principals with scoped, revocable `pbk_` credentials;
  `{domain}:read|write` scopes enforced on every API router.
- `audit_events` for all mutating calls, with `X-Request-ID` correlation and
  CSV export.
- `brain doctor` preflight with actionable failures.
- Truthful backup/restore (`pg_dump` + Neo4j export with checksums) and a
  restore verification drill.
- Per-run index completeness and embedding coverage verification.
- Docker-sandboxed change-lab validation (`--network none`, read-only rootfs,
  dropped capabilities), with an `unshare` fallback.
- MCP server with 13 tools (14 on the remote server).

### Changed

- Scheduled jobs run inside the worker
  ([ADR 013](docs/adr/013-worker-scheduler.md)); n8n is no longer used.
- The operator UI is the separate Next.js app in `apps/web`; the legacy
  server-rendered dashboard was removed.
- API docs (`/docs`, `/redoc`, `/openapi.json`) are disabled in production
  unless `API_DOCS_ENABLED` is set.
- Hashed dependency locks; CI on GitHub Actions (Python 3.12, Node 22) with a
  wheel build and clean-install smoke test.

### Fixed

- Durable Postgres fencing for worker write transactions: a worker that lost
  its lease cannot commit, and interrupted jobs are requeued.
- Job queue back-pressure (429 with `Retry-After`) and an in-app request body cap.

[Unreleased]: https://github.com/Perlitten/project-brain-public/compare/v0.9.0...HEAD
[0.9.0]: https://github.com/Perlitten/project-brain-public/releases/tag/v0.9.0
