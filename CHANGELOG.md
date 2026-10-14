# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

- Clarify self-reported MCP telemetry labels versus authenticated principals,
  native OAuth discovery/TLS and single-worker restart limits, and full/Lite
  queue cancellation and idempotency retention without claiming hosted-client
  verification or exactly-once side effects.

- Reconcile third-party notices with pinned Python and web dependencies,
  distinguish the TypeScript npm alias, include notices in runtime wheels,
  and check versions, licenses and packaged notice retention in CI.

- Add opt-in Lite source profile with durable SQLite memory, maintenance queues,
  local token-hash embeddings, SQLite graph adjacency and a disabled default LLM.
  Lite commands and local state are not in published v0.9.2 artifacts.
- Default agent context to `BRAIN_AGENT_CONTEXT_V2_MODE=on` and retrieval to
  `RETRIEVAL_V2_ENABLED=true`: bounded locators/runtime slices, linear per-file
  fusion, identifier/path handling and a bounded local BM25 companion. Both
  switches can be disabled for the legacy contract; no useful coding savings
  or learned semantic quality are established by the offline profile.
- Default moving-checkout context to `warn_and_serve`, with freshness warnings,
  bounded changed-source slices and throttled reindexing; retain `strict_block`.
- Make initial indexing write deterministic source summaries before optional
  asynchronous LLM enrichment. Summary availability is not evidence of model
  execution. Existing response-summary trimming was already present in v0.9.2.
- Exclude nested SQLite databases and their sidecars from root image contexts
  and source identity digests, even inside included application directories.
- Gate releases on a matching immutable tag/package version and the latest
  successful main-push CI run for that revision. Pass verified identities to
  all images, inventory installed API/worker/web dependencies, and publish
  the checksum-verified build artifacts instead of rebuilding them.
- Require an explicit, complete Brain configuration for full-mode backups and
  PostgreSQL restores; reject conflicting ambient database settings and require
  confirmation of the exact restore target. Recovery no longer silently uses a
  different default database. Neo4j reindexing and interrupted-job recovery
  remain separate operator steps.
- Split the FAQ into "Using Brain" (agents not calling Brain, wrong repository,
  stale index, stuck jobs, mock models, demo data, updating) and "Limits and evidence".
- Add a dashboard Help screen (`/help`): server version and models, common
  problems with links to the screens that fix them, the glossary and guides.
- Consolidate deployment and agent operations guides under `docs/operations/`,
  legal notices under `docs/legal/`, and contributor policies under `.github/`.
- Move production and experimental Compose configurations into `deploy/`;
  production commands use `--project-directory .` from the repository root to
  preserve `.env`, build contexts, bind mounts and host-local override semantics.
- Consolidate the remote stdio client template under `examples/`, retain root
  MCP discovery and repair deployment build-input manifests and navigation.
- Reconcile the live repository metadata with the productization GO assessment
  and use a portable empty env file in Compose tests.

## [0.9.2] - 2026-10-13

### Productization

- Shorten the public README and separate source installation, MCP setup, model
  providers, architecture, release verification and historical research.
- Validate current documentation versions, local links, anchors and supported
  installation commands automatically in CI.
- Keep synthetic example labels visible on desktop and mobile; restore panels
  and the memory map under reduced-motion preferences and improve keyboard access.
- Provide an isolated synthetic deployment guard that rejects backend bindings
  or operator credentials instead of exposing a live dashboard as a demo.
- Remove the separately deployed Next.js app and Python bytecode from runtime
  wheels while retaining frontend source and public templates in source archives.
- Archive unreferenced synthetic demonstrations, label historical evaluation and
  release tooling, and retain compatibility imports and frozen evidence.

## [0.9.1] - 2026-10-13

### Launch remediation

- Correct public repository, installation, CI, security and dashboard links.
- Publish the launch repairs in v0.9.1 with Python artifacts, checksums and an SBOM.
- Correct privacy, memory approval and unproven efficiency claims.
- Require explicit credentials and image tags in the experimental image compose
  configuration; fix Neo4j credential propagation and health-gated startup.
- Exclude ignored frontend outputs from Git indexing without hiding tracked
  source or weakening clean-commit provenance checks; skip symlink targets.
- Render dashboard data mode from runtime API bindings instead of serving a
  build-time demo snapshot in live containers; regression-test the existing
  runtime robots policy and server-only credential handling.
- Keep local credentials, npm dependencies and generated Next.js outputs out of
  the web Docker build context.

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

- Quality on a fresh install shows "No benchmark run yet" with a "Run the
  benchmark" button instead of "measurements unavailable"
  (`/api/web/quality` returns `{"available": false, "reason": "not_run"}`
  rather than HTTP 503 when no evidence file exists). A shipped evidence file
  with `"reference_run": true` is labelled "Reference run by maintainers". The
  period-selector note appears only on the overview, which has one.
- The mock provider is labelled "smoke test only, results not meaningful" in
  Setup, `brain doctor`, `.env.example` and the README, each with the one-line
  keyless path to real results (Ollama `nomic-embed-text`). Setup's "Connect
  AI models" step stays a warning on mock instead of turning green.

- Setup's "Choose the code to read" step lists the repositories Brain has
  actually indexed and makes one the active repository in a click. When
  `TARGET_REPO_PATH` was never indexed but another repository was (for example
  with `brain index --repo`), the step says so instead of reporting done; the
  default sample (Brain's own source) is labelled as such.
- `scripts/start.sh` starts the API, the worker and the web dashboard in one
  terminal; `scripts/bootstrap.sh` lists all three in its "Next" output, writes
  `--repo` into `TARGET_REPO_PATH`, and says so when it falls back to Brain's
  own source as a sample repository.
- `/health` reports worker liveness (`worker.running`, from the heartbeat each
  worker keeps in Redis). Without a worker, Setup blocks "Start Brain's
  services", the sidebar shows "worker down", and a queued job's toast says
  "Worker is not running. Start it with …" instead of waiting silently.
- First run: with no sign-in configured, the web dashboard opens without a
  password when it listens on loopback and is opened from this machine
  (`npm run dev` / `npm run start` now bind `127.0.0.1`; change it with
  `WEB_HOST`). Any other address, or a same-host reverse proxy that forwards a
  public `Host`, stays locked. `WEB_OPEN_MODE=false` locks loopback too.
- Telegram alert messages are in English.
- The web proxy returns a JSON 503 when the Brain API is unreachable.
- Release docs moved from `docs/release/0.9.0-rc1/` to `docs/release/0.9.0/`;
  historical design docs, benchmark results and pre-release reports removed.

### Fixed

- Lexical search finds code by the identifiers a query names: CamelCase and
  snake_case identifiers are kept whole and split into their words
  (`HTTPAdapter` → `httpadapter`, `http`, `adapter`), and a file that defines a
  symbol the query names leads the file channel, so "HTTPAdapter" returns
  `adapters.py`. Identifiers with underscores or trailing digits are no longer
  dropped from the keyword list.

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
  ([ADR 013](docs/adr/013-worker-scheduler.md)); the external orchestrator is no longer used.
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

[Unreleased]: https://github.com/Perlitten/project-brain-public/compare/v0.9.2...main
[0.9.2]: https://github.com/Perlitten/project-brain-public/releases/tag/v0.9.2
[0.9.1]: https://github.com/Perlitten/project-brain-public/releases/tag/v0.9.1
[0.9.0]: https://github.com/Perlitten/project-brain-public/releases/tag/v0.9.0
