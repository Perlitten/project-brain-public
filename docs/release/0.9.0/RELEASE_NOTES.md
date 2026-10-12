# Brain 0.9.0 — release notes

First public release of the single-user distribution of Project Brain: an
AI-native memory/navigation/context layer for coding agents, run per-user with
your own repositories and provider credentials.

Tag: `v0.9.0`. Changes after the tag are tracked in
[`CHANGELOG.md`](../../../CHANGELOG.md).

Pull request and issue numbers below (`#NN`) refer to the pre-public private
repository and do not resolve on GitHub.

## Highlights since 0.9.0 development

**Reliability**

- Durable Postgres fencing for every worker write transaction — a stale worker
  cannot commit after losing its lease (CAS on `worker_job_leases` inside the
  committing txn). Crash recovery verified live: SIGKILL mid-reindex → lease
  expiry → automatic requeue → attempt 2 completes; interrupted attempts are
  recorded truthfully, never as success.
- Truthful backup/restore: real `pg_dump` + Neo4j export with per-artifact
  checksums; verification drill restores into a scratch database and reports
  `verified`/`artifacts_validated`/`failed` honestly (live leg fixed in #95).
- Index completeness is recorded per run — discovered/indexed/unchanged/failed
  counts plus embedding coverage verification (`pgvector_coverage_pct`).
- Job queue back-pressure: bounded queue depth → 429 + Retry-After; request
  body cap enforced in-app.

**Security**

- Attributable principals with scoped, revocable `pbk_` credentials;
  `{domain}:read|write` enforced on every API router; legacy `PROJECT_BRAIN_API_KEY`
  still works as a synthetic admin.
- `audit_events` on all mutating calls, with `X-Request-ID` correlation end to
  end and CSV/read export.
- Change-lab validation runs in a real OS sandbox: docker backend verified —
  `--network none`, read-only rootfs, `--cap-drop ALL`, `no-new-privileges`,
  uid-mapped, pids/memory/cpu limits; `unshare` fallback; honest `unverified`
  status when neither is available.

**Install & ops**

- `brain doctor` preflight: python/.env/dirs/provider keys/service health with
  actionable failures (#94).
- Hashed `pip-compile` locks (`--require-hashes`); wheel build + clean-install
  smoke in CI (GitHub Actions: Python 3.12, Node 22).
- Test suite is hermetic to a configured `.env` (#96): 1508 tests, 0 failures
  with services configured.
- Dashboard: WCAG 2.2 AA pass (#91); admin page for principals/credentials (#92).
  The operator UI is now the separate Next.js app in `apps/web`.
- Scheduling moved into the worker (`brain/workers/scheduler.py`,
  [ADR 013](../../adr/013-worker-scheduler.md)); n8n is no longer used or required.

**Quality bar**

- mypy baseline 0 across `brain/`, `apps/`, `scripts/` (was 203).
- 1508 tests; ruff clean.

## Verified for this release

Pre-release validation covered install→first-task ~50s, restart persistence, incremental upgrade, live
backup/restore, crash recovery, docker sandbox, full suite.

## Known issues / manual steps

1. Backups only: `pg_dump`/`psql` required on PATH; must match server major.
2. `docker pull debian:bookworm-slim` needed for the docker sandbox backend.
3. The `.env.example` API key is the placeholder `change-me-in-production`.
   `scripts/bootstrap.sh` replaces it with a random value; if you copy the file
   by hand, rotate it yourself (production refuses to start with it — see
   `CHANGELOG.md`, Unreleased).
4. Real-provider paths (openai/anthropic/google/nvidia) configured but not
   verified with live keys in this release.
5. Remote MCP server, `unshare` backend and web UI session login are outside
   the verified single-user profile.
6. CI tests only Python 3.12 and Node 22; `scripts/bootstrap.sh` is POSIX-only
   (use WSL2 on Windows).

## Upgrade & rollback

See `UPGRADING_AND_ROLLBACK.md`. Schema migrations are idempotent and apply at
startup; there are no down-migrations — rollback restores from backup.

## License

MIT — see `LICENSE`. Third-party components keep their own licenses; see
`THIRD_PARTY_NOTICES.md`. The repository is public on GitHub
(Perlitten/project-brain-public).
