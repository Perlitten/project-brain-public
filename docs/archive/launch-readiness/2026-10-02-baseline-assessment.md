# Brain launch-readiness baseline — 2026-10-02

Reviewed commit: `master@3b8144eb94d75b85e2d534f2e13369d194a16bb2` (revalidated, all findings hold).
Environment: Ubuntu, Python 3.12.13, repo `.venv`. Docker daemon present; no live Postgres/Redis/Neo4j on this box during the run.

## Baseline measurements

| Check | Result | Command |
|---|---|---|
| Unit tests | **1380 passed, 2 skipped, 0 failed** (~40s) | `.venv/bin/pytest tests/ -q` |
| Skips | `test_neo4j_helpers_integration.py` (Neo4j not available); `test_v050_api_contracts.py::…` (no API key configured) | `pytest -rs` |
| Lint | clean | `.venv/bin/ruff check .` |
| mypy | **not run in CI** (dev extra only); repo-local `.venv/bin/mypy .` not executed for this baseline | — |
| CI on HEAD | **zero check runs / zero commit statuses** on `3b8144e`; last green `CI` push run = `88638ce` (#61, 2026-08-13, run 31666643322) | `gh api repos/Perlitten/Brain/commits/<sha>/check-runs` |
| Releases | GitHub Releases empty; `pyproject` declares `0.9.0`, `license = "MIT"`, **no LICENSE file, no SECURITY.md, no CONTRIBUTING.md** | `gh release list`, `ls` |

## Revalidated findings (all confirmed on the reviewed SHA)

| # | Sev | Finding | Evidence |
|---|---|---|---|
| F1 | P0 | Backups emit placeholder files and report success | `scripts/backup_runner.py:48-50,65` writes marker text when `pg_dump` missing/fails or n8n sqlite absent; manifest still records the artifact and prints `Backup completed successfully`. No Neo4j artifact exists at all. |
| F2 | P0 | Restore drill reports `SUCCESS` without restoring anything | `scripts/restore_verification_drill.py:60-77` only verifies checksums and *optionally* counts sqlite tables when size>100; a 28-byte marker yields `sqlite_tables_found: 0` and `restore_drill_status: SUCCESS`. Never restores PostgreSQL or Neo4j. |
| F3 | P0 | Job lease never renewed; fencing token not passed to dispatcher; status writes unfenced | `renew_lease` defined (`brain/workers/queue.py:251`) but never called. `worker.py:70` calls `execute_job(job_type, params)` without job_id/token. `validate_task_fencing` (`tasks.py:42`) is called only by a test and a script, never by the dispatcher. `update_status`/`schedule_retry`/`start_attempt`/`recover_stale` perform no ownership check — a stale worker can overwrite terminal state after takeover. `ack` alone verifies the token. |
| F4 | P0 | Indexer marks runs `completed` despite per-file/graph failures | `brain/indexers/file_indexer.py:563-590`: `index_file` exceptions logged and swallowed, then `indexing_status="completed"` and `run.status="completed"` unconditionally. No failure counts on the run/repo record. |
| F5 | P0 | Lab runner network isolation asserted, not enforced | `brain/lab/models.py:22` `NETWORK_ISOLATION_STATUS = "unverified"`; `engine.py` comments confirm policy-only. |
| F6 | P0 | Single shared credentials; no org boundary | `settings.py`: one `PROJECT_BRAIN_API_KEY`, one `BRAIN_DASHBOARD_USER`/`_PASSWORD`; `models.py` has no organization/tenant/owner columns on Repository/File/memory rows. |
| F7 | P0 | Floating runtime deps; CI lacks Neo4j + mypy | `pyproject.toml` deps mostly unpinned (only `mcp==1.29.0`, `numpy>=1.26,<3` bounded). `ci.yml` provides real Postgres+Redis but `NEO4J_URI=bolt://127.0.0.1:1` stub and no mypy/typecheck step. |
| F8 | P0 | No LICENSE; empty Releases; no checks on release SHA | See baseline table. Branch protection unverified (ruleset listing empty, protection reads 403 — needs admin view). |

## Deployment model (scope corrected by owner, 2026-10-02)

Brain is a distributable **single-user harness**: each user runs their own
instance with their own repositories and provider credentials. The supported
launch tier is a local or single-VPS install (docker compose services + local
or containerized API/worker). Organization management, SSO/SAML/SCIM,
enterprise roles, multi-tenant SaaS isolation, and billing are **out of
scope** — they are not release blockers for this product, and the SSO track
was stopped in-flight by the owner. The existing principals, scoped
credentials, and audit protections stay: they are the single-user's
per-agent key management, not team features. **Release criterion:** someone
unfamiliar with the project can install and successfully use the harness
without help from its author.

## Capability inventory (initial classification — to be completed against route/CLI/MCP maps in backlog B1)

- **Supported (evidence: tests + prod compose):** HTTP API w/ API-key auth, dashboard w/ user/pass auth + CSRF + nginx rate limiting, MCP stdio adapters (local + HTTP-backed env-cred), pgvector retrieval, file card/symbol route, freshness/exact-revision checks, worker queue w/ idempotency + retry + stale recovery, release identity (`deploy/doctor.sh stale-scan`, `.project-brain-release`), backup manifest+checksum scaffolding, eval/promotion machinery (golden tasks, champion/challenger, LFM gates).
- **Experimental / gated:** lab/change-execution runner (isolation unverified), LFM late-interaction (shadow/canary only), worker pools v2 (`BRAIN_WORKER_POOLS_V2_ENABLED`), autonomy GoalSession paths.
- **Incomplete (must not ship as supported):** backup/restore (F1/F2), job fencing (F3), index completeness states (F4), enterprise multi-user admin (F6), release artifacts (F8).

## Risk register (top)

| Risk | Impact | Current state |
|---|---|---|
| Silent data loss presented as recoverable | customers trust a backup that restores nothing | F1/F2 — no detection, no live restore proof |
| Duplicate authoritative job execution | corrupted index/memory writes during restarts | F3 — lease expiry = 60s, jobs run longer |
| Harness/freshness contracts fed by partial index | wrong "complete" answers | F4 — no failed/degraded index state |
| Untrusted repo code runs beside prod secrets | host compromise | F5 — policy-only isolation |
| Cross-org/private-code exposure | launch blocker for any shared deployment | F6 — no tenant boundary anywhere |

## Ordered implementation backlog

Legend — *Acceptance* = runnable verification; *Rollback* = revert path. PRs stay small and independently reviewable.

### B0 — Baseline & contract (PR-doc)
**Files:** `docs/launch-readiness/*`. Observable: this document + launch matrix accepted as the tracking artifact.
Acceptance: doc lists every revalidated finding with evidence and commit. Rollback: delete docs dir.
*Status: this PR.*

### B1 — Truthful backups + honest restore drill (P0, next)
**Files:** `scripts/backup_runner.py`, `scripts/restore_verification_drill.py`, `tests/test_backup_restore.py`.
Observable failure: backup with missing `pg_dump` produces a 53-byte marker + `Backup completed successfully`; drill reports `SUCCESS` with zero tables.
Remediation: required-artifact statuses in manifest (`ok|failed|absent`), no marker files ever, non-zero exit on any required failure; Neo4j graph export artifact; drill performs real `psql` restore when a target URL is provided, enforces `sqlite_tables>0`, and distinguishes `verified` vs `artifacts_validated` vs `failed`.
Acceptance: `python scripts/backup_runner.py --output /tmp/b` exits 1 without pg_dump; seeded restore drill reports `verified`; `pytest tests/test_backup_restore.py`.
Rollback: revert files; no schema/state change.
*Depends: none.*

### B2 — Job ownership end-to-end (P0)
**Files:** `brain/workers/{worker.py,queue.py,tasks.py}`, `tests/test_queue_concurrency_fencing.py`, `tests/test_worker_queue.py`.
Observable failure: job >60s loses lease; `recover_stale` requeues → duplicate execution; stale worker can still `update_status(COMPLETED)`.
Remediation: lease-renewal task around `execute_job`; pass `(job_id, fencing_token)` into dispatcher and validate before durable mutations; fence `update_status`/`schedule_retry`/`ack` transitions; abort on `StaleWorkerFencingError`.
Acceptance: two workers race a >lease job — exactly one commits (new concurrency test); `pytest tests/test_queue_concurrency_fencing.py tests/test_worker_queue.py`.
Rollback: revert; queue state compatible (lease keys additive).
*Depends: none. Note: full durable fencing on the DB commit itself may need a generation/outbox design — capture as B2b if Redis check + abort proves insufficient in tests. **B2b delivered in https://github.com/Perlitten/Brain/pull/70:** `worker_job_leases` table; claim/renew/release CAS under `SELECT FOR UPDATE`; `assert_db_fence` inside the committing transaction (file indexer's 7 write txns); `check_db_fence_open` on the result-commit boundary.*

### B3 — Index completeness states (P0)
**Files:** `brain/indexers/file_indexer.py`, `brain/database/models.py` (run counters), `tests/test_file_indexer*.py`.
Observable failure: run with 3 failing files reports `completed`.
Remediation: count processed/failed/unchanged; statuses `completed|degraded|failed`; degraded never satisfies exact-revision harness contract; last-good generation preserved.
Acceptance: injected per-file failures yield `degraded` with counts; `pytest tests/ -k indexer`.
Rollback: revert; add-only columns nullable.
*Depends: none.*

### B4 — Execution isolation (P0, security)
**Files:** `brain/lab/{engine,models}.py`, new sandbox runner, hostile-fixture tests.
Remediation: disposable container or documented equivalent — non-root, minimal mounts, no prod creds, resource limits, net denied by default, verified cleanup.
Acceptance: hostile fixtures (fs escape, secret read, net egress, fork bomb) all confined; `NETWORK_ISOLATION_STATUS` becomes `enforced:<mechanism>` only with test evidence.
Rollback: gate lab runner off by default.
*Depends: none; may need infra auth for images.*

### B5 — Repeatable builds + real CI (P0 release)
**Files:** `pyproject.toml`, lockfile (uv/pip-tools), `.github/workflows/ci.yml`, Dockerfiles.
Remediation: hashed lock + update workflow; CI adds Neo4j service + mypy + migration test + wheel build/install check; required checks on candidate SHA.
Acceptance: clean `pip install dist/*.whl` runs CLI/MCP entrypoints; CI green at exact SHA.
Rollback: revert lock; deps stay floating-only.
*Depends: none.*

### B6 — License/distribution resolution (P0 release)
**Files:** `LICENSE`, `SECURITY.md`, `CONTRIBUTING.md`, release workflow, history scan.
Observable: MIT declared, no file; no releases.
Acceptance: license decision by owner recorded; signed artifacts + SBOM + provenance on tag. Rollback: pull release.
*Depends: owner decision; secret-scan of history before any publication.*

### B7 — Principals, scoped authz, org boundary (P0 security, staged)
**Files:** `settings.py`, auth middleware, `models.py` (+migration), routers, MCP authz.
Remediation: attributable principals → scoped credentials → central policy → org ownership on primary models; audit events.
Acceptance: revoked creds denied; cross-scope reads/write tests fail closed.
*Depends: B5 migrations; larger staged effort.*
Delivered: B7a principals + hashed scoped credentials + attribution (PR #71), B7b `{domain}:read|write` enforcement on every API router + self-sufficient `require_scope` + route-coverage regression test (PR #72), B7c `audit_events` on mutating calls incl. denied attempts (PR #73), B8a `X-Request-ID` correlation into logs + audit rows (PR #74). Removed: B7d org columns + tenant enforcement (out of scope — single-user product).

### B8–B13 (P1, subsequent sessions)
B8 reliability/observability/quotas (health split, correlation, alerts+runbooks, budgets). B9 retrieval quality eval (frozen holdouts, adversarial cases, matched budgets). B10 operator UX completion (dashboard.py split behind behavior tests, WCAG 2.2 AA, 502/503 pages). B11 credential administration for the owner's agents (audit export, principal/credential lifecycle, per-agent scoped keys); *org users/roles/SSO removed by the 2026-10-02 scope correction*. B12 tenant isolation qualification — *out of scope*. B13 staged launch (install journey validation, suite-on-services, RC assembly). B5b mypy debt cleanup (203 → 0; ratchet gate added in PR #69 prevents growth).
Delivered: B5b mypy zeroed #75–#78; B8 budgets + alert catalog #79; B9 frozen holdout + adversarial traps #80; B10 nginx 502/503/504 page #81 + dashboard.py split #87–#90 + WCAG pass #91; B11 audit read/CSV export #82, principal/credential lifecycle API #83 + admin UI #92; bridge auth #84; MCP scoped-key docs #85.

## Launch matrix (live tracking — updated per PR)

| Requirement | Profile | Owner | Status | Evidence | Commit | Risks |
|---|---|---|---|---|---|---|
| Truthful backup/restore | self-hosted | devin | merged | https://github.com/Perlitten/Brain/pull/64 | merged | live restore still needs services |
| Job fencing | self-hosted | devin | merged | https://github.com/Perlitten/Brain/pull/65 | merged | — |
| Durable commit fencing (B2b) | self-hosted | devin | merged | https://github.com/Perlitten/Brain/pull/70 | merged | all worker-reachable write txns fenced |
| Index completeness | self-hosted | devin | merged | https://github.com/Perlitten/Brain/pull/67 + #66 | merged | live index e2e needs services |
| Exec isolation | self-hosted | devin | merged | https://github.com/Perlitten/Brain/pull/68 | merged | docker backend unverified (no image/daemon pull) |
| Reproducible CI/release | all | devin | merged | https://github.com/Perlitten/Brain/pull/69 | merged | mypy debt zeroed via #75–#78 |
| License/distribution | all | owner | blocked-owner | — | — | legal decision |
| Principals + scoped credentials (B7a) | dedicated+ | devin | merged | https://github.com/Perlitten/Brain/pull/71 | merged | legacy key still resolves to admin |
| Domain scope enforcement (B7b) | dedicated+ | devin | merged | https://github.com/Perlitten/Brain/pull/72 | merged | — |
| Audit events (B7c) | dedicated+ | devin | merged | https://github.com/Perlitten/Brain/pull/73 | merged | — |
| Request correlation (B8a) | dedicated+ | devin | merged | https://github.com/Perlitten/Brain/pull/74 | merged | — |
| mypy debt zeroed (B5b) | all | devin | merged | https://github.com/Perlitten/Brain/pull/78 | merged | ratchet at 0 |
| Resource budgets + alert catalog (B8) | self-hosted | devin | merged | https://github.com/Perlitten/Brain/pull/79 | merged | — |
| Frozen eval holdout + traps (B9) | all | devin | merged | https://github.com/Perlitten/Brain/pull/80 | merged | trap metrics report-only until gated |
| nginx 502/503/504 page (B10) | self-hosted | devin | merged | https://github.com/Perlitten/Brain/pull/81 | merged | — |
| Audit read/CSV export (B11) | dedicated+ | devin | merged | https://github.com/Perlitten/Brain/pull/82 | merged | — |
| Principal/credential admin API (B11) | dedicated+ | devin | merged | https://github.com/Perlitten/Brain/pull/83 | merged | — |
| Bridge scoped auth (#84) | dedicated+ | devin | merged | https://github.com/Perlitten/Brain/pull/84 | merged | poller needs a minted `bridge:write` key |
| MCP scoped-key docs | all | devin | merged | https://github.com/Perlitten/Brain/pull/85 | merged | — |
| dashboard.py split slices 1–3 (B10) | self-hosted | devin | merged | https://github.com/Perlitten/Brain/pull/87 #88 #89 | merged | remaining pages follow |
| WCAG 2.2 AA pass (B10) | single-user | devin | open | https://github.com/Perlitten/Brain/pull/91 | open | — |
| Principal admin UI (B11) | single-user | devin | open | https://github.com/Perlitten/Brain/pull/92 | open | — |
| Org boundary (B7d) | — | — | out-of-scope | — | — | single-user scope: no org tier |
| Tenant isolation | — | — | out-of-scope | — | — | single-user scope: no shared SaaS |
| SSO / team roles (B11 rem.) | — | — | out-of-scope | — | — | single-user scope; WIP parked uncommitted |

## GO/NO-GO assessment (2026-10-02, rescoped to single-user distribution)

This snapshot covers 26 merged PRs (#63–#85, #87–#89). Authors report 1455 tests, ruff clean, and mypy baseline 0 before merge. Merge-host checks passed for ten dependency-independent queue tests, twelve holdout eval tests, and preservation of all 91 dashboard functions and route decorators across the three splits. Full API pytest and mypy could not run on the merge host because macOS denied loading their installed native libraries. GitHub Actions is disabled repo-wide; a complete CI run remains unverified.

- **Single-user self-hosted install (the only launch profile): CONDITIONAL GO.** All P0 engineering merged: truthful backups (#64), job fencing Redis+durable (#65, #70), index completeness (#67, #66), lab sandbox (#68), reproducible build/CI gates (#69), principals+scopes+audit+correlation (#71–#74), mypy zeroed (#75–#78), resource budgets + alert catalog (#79), eval holdout (#80), 502/503 pages (#81), audit export (#82), credential admin API+UI (#83, #92), scoped bridge/MCP auth (#84, #85), dashboard split (#87–#90), WCAG pass (#91). Remaining gates before an RC tag, in brief order: (a) install/packaging friction — minimal supported runtime, config validation, safe defaults, agent-connect docs; (b) full-journey validation in a clean supported environment (install → configure provider credentials → connect agent → index a repo → complete a task → restart → upgrade without state loss), with recorded versions/commands/time-to-first-task; (c) the full suite on integrated master against real Postgres/Redis/Neo4j plus live restore drill, long-job recovery, index completeness and the real docker sandbox backend; (d) versioned RC assembly (install docs, support matrix, example config without secrets, release notes, migration/rollback, blocker list); (e) owner license/distribution decision (B6).
- **Enterprise team / shared SaaS: not applicable — removed from scope** by the owner on 2026-10-02. The principals/scoped-credential/audit machinery stays as the single user's per-agent key management; org columns, tenant isolation, SSO and team roles are not release requirements and no GO/NO-GO applies to them.

Immediate next steps: config validation + install friction PRs; full-journey validation on real services; RC assembly. Owner: license call (B6) — nothing publishes without it.
