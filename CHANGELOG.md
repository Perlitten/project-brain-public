# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

- Add the first slice of bounded team execution (M2), **off by default**. With
  `BRAIN_EXECUTION_ENABLED`, an execution root and an operator tool allowlist set,
  a team administrator can grant a Contributor or Reviewer execution on one project
  (`brain team execution grant`); the requester of a run can then ask Brain to run a
  fixed list of allowlisted tools in a fresh clone at the task's base SHA. Every tool
  call and ledger write re-checks the whole authority chain, so a revocation stops
  the run; a lease with a fencing token keeps a crashed or expired worker from
  committing. Secrets are passed by reference and never stored. The subprocess
  backend is not containment and refuses egress. See
  [bounded team execution](docs/operations/TEAM_EXECUTION.md).
- Add optional, off-by-default OpenID Connect single sign-on for team members
  ([TEAM_SSO.md](docs/operations/TEAM_SSO.md)): authorization-code flow with
  PKCE, state, nonce and a browser binding; ID tokens validated against the
  provider's JWKS (`pyjwt[crypto]`, now a direct dependency). An invitation
  that names an email can be accepted at `/auth/oidc/start?invitation=...`
  only by the account whose verified email matches; bound members get
  **Sign in with SSO** on the dashboard with the same session and scopes as a
  member key. `BRAIN_OIDC_REQUIRE_FOR_INVITATIONS` refuses token-only
  acceptance. Operators bind existing members with
  `brain team member sso-bind`. No auto-provisioning.

- Team mode is an **experimental preview** on `main` (not in v0.9.2, not
  deployed, not piloted). Add the [team mode walkthrough](docs/TEAM.md) from
  `brain team init` to revocation, with its known limitations, and a team-mode
  section in [SECURITY.md](SECURITY.md): project isolation is in scope for
  reports, invitation tokens are bearer secrets, and revoking membership does
  not revoke keys. Docs no longer describe Brain as single-user only.

- Give member keys from `brain team member add --mint-key` and from
  invitations the same scopes as dashboard keys, so they open the dashboard
  and can search, build context and ask; team-admin keys also get
  `setup:write` and `operations:write`. Members read global decisions and
  rules plus those of repositories they can read (`/decisions`, `/rules`,
  `brain://decisions`, `brain://rules`), and non-admin members see a dashboard
  limited to their projects. Keys minted earlier keep their old scopes and must
  be re-issued (#124).

- Restoring a team snapshot with the supported script leaves a restore fence:
  no named credential, shared key, OAuth refresh or invitation works until
  `brain team reconcile --attest <digest>`, which by default revokes every
  restored credential, OAuth refresh token, run delegation and pending
  invitation (#122).

- Let dashboard members sign in with their own `pbk_` member key in team
  state; every dashboard call is sent with that key. Add the admin Team page to
  add members, issue a key (shown once), set project roles and revoke
  membership (#121).

- Add single-use team invitations through the API and `brain team invite`
  (create, list, revoke, accept): a `pbi_` bearer token, stored hashed, turns
  its holder into a verified member with a named credential. The email is never
  checked; acceptance is throttled per API process (#119).

- Unify the personal and team APIs: a Brain is a team of one until its first
  verified member is added, and boundaries switch on from then. The
  `TeamBoundary` 403 wall is gone; `/health` reports `mode` as `solo` or
  `team`. Search, ask, context packs, impact, diff review, decisions, rules,
  observations, lessons, job submission and the repository-scoped web, graph,
  impact, drift, coupling and remediation routers authorize the repository's
  project per request (`read`, `knowledge.propose`, `run.request`); with no
  members every authenticated caller keeps full access, with members an
  unattached repository needs a team administrator and the shared key or
  keyless access answers `Named credential required`. The `team_*` MCP tools
  are served next to the personal tools on one server, and workers and the
  scheduler keep running for a team (queued jobs are re-authorized before they
  run). Team runs are recorded but never executed: no executor exists yet.
  `BRAIN_TEAM_MODE` is now a deprecated strictness alias (treat an empty team
  as a team) that logs a warning.

- Make experimental team mode provisionable without manual SQL: `brain team`
  CLI and `team:admin` API routes create the team, projects, verified members
  and repository bindings idempotently. OAuth clients can hold `team:*` scopes
  only when a verified member approves them, within that member's live project
  roles; the token then belongs to the member's principal and refresh
  re-checks membership. Harness status/event writes attribute the authenticated
  principal instead of a client-supplied actor; decisions and rules record the
  author principal (nullable `author_principal_id`, migrated automatically) and
  `system_global` writes through the API need the `core:admin` scope (held by
  the legacy shared key and `*` credentials) and answer 403 otherwise.

- Show edits omitted by the latest index run in the dashboard, sidebar and
  Setup. Keep unknown counts distinct from zero and label the observation as
  index-run provenance rather than live filesystem monitoring.
- Disable Telegram's test-message control until both a saved bot token and
  chat ID are configured, with an explanation instead of a doomed request.

- Replace the all-at-once code map with collapsed top-level areas, searchable
  modules, a minimum edge-weight filter and persistent one/two-hop focus.
  Tests are hidden by default; the map fits desktop and narrow mobile screens
  without horizontal scrolling. The complete Connections table is unchanged.

- Add default-off single-team project/task/run authority foundations with live
  verified-identity membership, narrowed delegation, stable repository references,
  provenance and idempotency. Team runs are recorded, not executed; this is not
  a team-ready MVP. (The personal-API wall this entry introduced was replaced by
  the unified model above.)
  Personal/full and personal/Lite retain their existing defaults.

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
- Align the unreleased source path on Python 3.12 and Node 22; bootstrap and
  the API image now install the same hash-locked dependency graph as CI.
  Python 3.10/3.11 remain available on the published v0.9.2 tag, not on this
  new source revision.
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
