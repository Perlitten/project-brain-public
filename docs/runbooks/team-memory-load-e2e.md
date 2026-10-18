# Runbook: multi-user team memory load e2e

`scripts/team_memory_load_e2e.py` drives team-mode **memory** with many principals at once against a
real local stack, then checks the memory invariants and writes a report. It complements the
functional `scripts/team_e2e_check.py` ([runbook](team-e2e-check.md)): that one walks the team
lifecycle once, this one puts concurrent load on decisions, rules, proposals, the personal tier,
consolidation, revocation and coordination leases.

Findings of the first runs: [docs/reports/team-memory-load-e2e-2026-10-18.md](../reports/team-memory-load-e2e-2026-10-18.md).

The harness and original report were imported from commit
`993eee81a0ab9c74d0b232689532fa35ca2d0c69`; they were absent from main after PR156.
Reports now include `source.commit` and `source.dirty`. Run from a clean commit
for release evidence. Personal retirement is a required check, not a known gap.
The original findings document the earlier code, not the current fixes.

Reports also record `runtime.platform` and `runtime.python`. Native Windows supports
one API worker only. Run `--api-workers 2` (or higher) from a Linux Python runtime,
including WSL or a Linux container. Running a Windows Python executable from WSL
still uses Windows and is rejected before any fixture is created. Do not silently
reduce the worker count when validating concurrency.

A two-worker Windows run reproduced a 120-second OAuth GET timeout before ASGI
admission: the worker event loop was blocked in synchronous Winsock `accept()`.
The same failure reproduced without Brain, PostgreSQL or OAuth in a minimal
uvicorn ASGI server. Forcing Proactor is not a valid workaround for a shared
listener: a second worker failed with `WinError 87` registering it with IOCP.
See [the diagnostic evidence](../reports/team-locking-regressions.md#windows-multi-worker-transport).

## What it runs

- The real API (`uvicorn apps.api.main:app`, `--api-workers N`) and the real worker
  (`brain.workers.worker`), started by the script from the checkout.
- A throwaway PostgreSQL 16 with pgvector and a throwaway Redis. The script creates and drops
  only the `brain_e2e_memload` and `brain_e2e_memload_restore` databases on that server.
- Mock LLM and embedding providers (`LLM_PROVIDER=mock`, `EMBEDDING_PROVIDER=mock`). Neo4j is not
  started; the graph is reported unavailable and nothing here depends on it.
- A generated shared key and generated member credentials. The project's `.env` is never read.
  Child processes set `BRAIN_DISABLE_DOTENV=true` and receive their complete configuration from the harness,
  including uvicorn worker processes started by `--api-workers`.

The team: N principals (minimum 12) over three projects (`core`, `beta`, `gamma`): admins,
reviewers, contributors, a reader, a personal-tier pair and an OAuth client acting for a member.
Members join through the CLI invite, API invitations (a concurrent accept burst) and
`brain team member add` + OAuth.

## Scenarios and checks

| Phase | What happens | Invariants checked |
|---|---|---|
| 0-setup | team bootstrap, onboarding burst, invitation throttle | members joined; no 5xx on concurrent accepts; 429 after the per-address limit; shared key refused in team state |
| load | every member except the revokee runs `--ops` mixed REST/MCP operations concurrently (reads, search, context packs, proposals, personal learnings, decisions, activity) | no cross-project or personal-tier marker in any reply; foreign reads and writes refused; readers write nothing shared |
| 2-cas | `--cas-rounds` rounds of `--cas-writers` writers on one decision and one rule with the same `expected_version`; blind writes; concurrent approvals of several proposals; concurrent creates of one title | exactly one winner per round, the rest 409; linear supersede chain without duplicate versions or unreachable rows |
| 3-governance | member proposals, reviewer approve/reject, member and self-approval | 403 for members and self-approval; approvals only by authority |
| 4-consolidation | one principal emits 8 L1 events over two tasks; two principals emit the same claim; a consolidation pass after each | G2 counts distinct principals: one principal never promotes, two do |
| 5-freshness | freshness envelope on reads; origin lag through a local bare git origin | envelope present and sane |
| 6-leases | `--lease-contenders` contenders claim/release one coordination lease `--lease-churn` times | no double grant, no overlap in time |
| 7-provenance | items and activity events | each names the principal that wrote it |
| 8-revocation | a pg_dump snapshot is taken; a member writes from `--revoke-threads` threads; the admin revokes her after `--revoke-after` s; traffic continues `--revoke-tail` s | first revoke attempt succeeds; nothing authored by her dated after `revoked_at`; no request honoured after the revoke returned (REST and MCP); pending proposals withdrawn |
| 9-restore-fence | the pre-revocation snapshot is restored into `brain_e2e_memload_restore` and served by a second API | the restore does not resurrect the revoked member or her key |
| post-revoke reads | a short read sweep by every remaining member | still no leak |

Known product gaps are recorded as `known_gaps`, not failures. Every failed check that reveals a
product defect adds a named entry to `violations` with examples and a pointer to the code.

## Prerequisites

- The project venv with dev dependencies (`.venv/Scripts/python` on Windows, `.venv/bin/python`
  elsewhere).
- Docker (on Windows: Docker inside WSL works, e.g. `wsl -d Ubuntu-24.04 docker`).
- Free local ports: 5444 (PostgreSQL), 6390 (Redis), 18571 and 18572 (API and restore API).

## One-command run

```bash
# Linux/macOS
PYTHON=.venv/bin/python scripts/team_memory_load_e2e.sh --users 12 --ops 50

# Windows (Git Bash), Docker inside WSL
DOCKER="wsl -d Ubuntu-24.04 docker" PYTHON=.venv/Scripts/python \
    scripts/team_memory_load_e2e.sh --users 12 --ops 50
```

The wrapper starts `pgvector/pgvector:pg16` on `127.0.0.1:5444` and `redis:7-alpine` on
`127.0.0.1:6390` with a generated password, runs the e2e with the given arguments and removes both
containers on exit. Higher concurrency:

```bash
DOCKER="wsl -d Ubuntu-24.04 docker" PYTHON=.venv/Scripts/python \
    scripts/team_memory_load_e2e.sh --users 36 --ops 50 --api-workers 2
```

A 12├ù50 run takes about 4 minutes, 36├ù50 about 8 minutes. At 36 users the onboarding burst and the
`teams` row lock can stall single requests for the full 120 s client timeout (see the findings
report); setup retries those, later phases record them as transport errors.

## Manual run against containers you keep

```bash
PW=$(python -c 'import secrets; print(secrets.token_hex(16))')
docker run -d --name brain-memload-pg -e POSTGRES_PASSWORD="$PW" -p 127.0.0.1:5444:5432 \
    pgvector/pgvector:pg16 -c max_connections=300
docker run -d --name brain-memload-redis -p 127.0.0.1:6390:6379 redis:7-alpine

export TEAM_MEMORY_E2E_DATABASE_URL="postgresql://postgres:$PW@127.0.0.1:5444/postgres"
export TEAM_MEMORY_E2E_REDIS_URL=redis://127.0.0.1:6390/0
export TEAM_MEMORY_E2E_DOCKER=docker               # or "wsl -d Ubuntu-24.04 docker"
export TEAM_MEMORY_E2E_PG_CONTAINER=brain-memload-pg
.venv/bin/python scripts/team_memory_load_e2e.py --users 12 --ops 50
```

Without `--docker-cmd`/`--pg-container` the restore-fence scenario is skipped and listed under
`not_exercised`.

### Flags

| Flag | Default | Meaning |
|---|---|---|
| `--database-url` | `$TEAM_MEMORY_E2E_DATABASE_URL` | admin URL of the throwaway server (`.../postgres`); only `brain_e2e_memload*` databases are touched |
| `--redis-url` | `$TEAM_MEMORY_E2E_REDIS_URL` or `redis://127.0.0.1:6390/0` | throwaway Redis |
| `--users` | 12 | principals (minimum 12) |
| `--ops` | 50 | operations per user in the load phase |
| `--api-workers` | 1 | uvicorn workers for the main API |
| `--cas-rounds`, `--cas-writers` | 5, 6 | CAS contention |
| `--lease-contenders`, `--lease-churn` | 8, 15 | lease contention |
| `--revoke-threads`, `--revoke-after`, `--revoke-tail` | 4, 1.5, 2.0 | revocation while writing |
| `--docker-cmd`, `--pg-container` | env | how to reach `pg_dump`/`psql` for the restore fence |
| `--out` | `artifacts/team-memory-e2e/<UTC>` | output directory |
| `--keep-db` | off | leave `brain_e2e_memload` for inspection |
| `--seed` | 20261018 | operation mix seed |

## Outputs

- `REPORT.md` ΓÇö checks (PASS/FAIL/GAP), violations with examples, per-endpoint latency
  (p50/p95/p99/max) and status counts, load throughput, not-exercised notes.
- `report.json` ΓÇö the same, machine-readable: `checks`, `violations`, `metrics.endpoints`,
  `metrics.phases` (throughput), `metrics.status_distribution`, `results` (CAS rounds, revocation
  attempts, admin visibility, roster), `not_exercised`.
- `api-*.log`, `worker.log` ΓÇö server logs. Secrets are redacted from the reports; the SQL dumps are
  deleted after the run.

Exit code: 0 when every check passed (known gaps allowed), 1 otherwise. The first runs exit 1:
they find the product defects listed in the findings report.

## Notes

- Each member gets its own `X-Forwarded-For` address from TEST-NET-2 (`198.51.100.0/24`) and the API
  runs with `BRAIN_TRUSTED_PROXY_IPS=127.0.0.1`, so per-address throttles (invitation accept) see
  separate clients, as in production behind a proxy.
- The script stops the API and worker as process trees (`taskkill /T` on Windows), because
  `uvicorn --workers` children otherwise outlive the run and keep serving the port. It refuses to
  start when the API port already accepts connections.
- Onboarding retries (invitation create, accept, CLI `member add`) are counted and still fail the
  onboarding check; they only let the rest of the run proceed.
- Consolidation evidence lives in its own repository (`/e2e/alpha-memory`) and the two G2 scenarios
  run as separate consolidation passes. The empty SHA-256 slices in the mock embedder are fixed;
  these deterministic scenarios still do not validate live provider behaviour.

## Pytest

```bash
.venv/Scripts/python -m pytest tests/test_team_memory_load_e2e.py -q
```

The helper tests run everywhere. `test_stack_run_keeps_isolation_invariants` runs a short stack
pass (`--users 12 --ops 10`) only when `TEAM_MEMORY_E2E_DATABASE_URL` is set (plus the Redis,
docker and container variables above). It asserts the isolation and authority invariants (no leak,
no foreign read or write, no self-approval, no request honoured after revoke, restore fence, no
lease double grant), not the known concurrency defects.

## Teardown

The wrapper removes its containers. After a manual run:

```bash
docker rm -f brain-memload-pg brain-memload-redis
```
