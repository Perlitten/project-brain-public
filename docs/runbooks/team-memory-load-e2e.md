# Runbook: multi-user team memory load e2e

`scripts/team_memory_load_e2e.py` drives team-mode **memory** with many principals at once against a
real local stack, then checks the memory invariants and writes a report. It complements the
functional `scripts/team_e2e_check.py` ([runbook](team-e2e-check.md)): that one walks the team
lifecycle once, this one puts concurrent load on decisions, rules, proposals, the personal tier,
consolidation, revocation and coordination leases.

Findings of the first runs: [docs/reports/team-memory-load-e2e-2026-10-18.md](../reports/team-memory-load-e2e-2026-10-18.md).

Reports now include `source.commit` and `source.dirty`. Run from a clean commit
for release evidence. Personal retirement is a required check, not a known gap.
The original findings document the earlier code, not the current fixes.

Reports also record `runtime.platform` and `runtime.python`. Windows uses one
listening port per API process (the #159 workaround); Linux uses uvicorn workers
on one port. Do not silently reduce the worker count when validating concurrency.

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

A 12×50 run takes about 4 minutes, 36×50 about 8 minutes. A request that hangs for the full 120 s
client timeout is recorded as a transport error (status `0`); setup retries those. On Windows the
120 s stalls came from `uvicorn --workers`, not from the database (next paragraph).

The revocation check compares PostgreSQL-stamped `revoked_at` and `created_at`
directly. It does not apply host clock skew: both timestamps use the database
clock. The legacy `db_clock_skew_ms` report field is zero.

`--api-workers N` on Windows starts N single-process APIs on ports `api-port + 100 * i` and spreads the
client threads across them, instead of `uvicorn --workers N`. Every process gets the first address as
`BRAIN_PUBLIC_URL`, and CLI, OAuth and MCP calls go to that first process. uvicorn workers share one
listening socket, and on Windows they then run a selector event loop. When two workers race for one
connection, the loser's non-blocking `accept()` can block inside Winsock. That freezes that worker's
event loop, with every request already on its keep-alive connections, until the next new connection
arrives, which in this load run was often a client's 120 s timeout. A proactor loop does not help,
because a shared socket attaches to only one IOCP. Linux keeps `uvicorn --workers N`.

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
| `--api-workers` | 1 | API processes for the main API (`uvicorn --workers`; on Windows one port per process, see above) |
| `--cas-rounds`, `--cas-writers` | 5, 6 | CAS contention |
| `--lease-contenders`, `--lease-churn` | 8, 15 | lease contention |
| `--revoke-threads`, `--revoke-after`, `--revoke-tail` | 4, 1.5, 2.0 | revocation while writing |
| `--docker-cmd`, `--pg-container` | env | how to reach `pg_dump`/`psql` for the restore fence |
| `--out` | `artifacts/team-memory-e2e/<UTC>` | output directory |
| `--keep-db` | off | leave `brain_e2e_memload` for inspection |
| `--seed` | 20261018 | operation mix seed |

## Outputs

- `REPORT.md` — checks (PASS/FAIL/GAP), violations with examples, per-endpoint latency
  (p50/p95/p99/max) and status counts, load throughput, not-exercised notes. Two sections are
  meant for before/after comparisons:
  - **CAS outcomes**: one row per CAS round (decisions, rules, the create race, concurrent
    approvals, approve vs reject) with `expected_version`, the winner count, the losers' status
    codes (`{"409": 5}` is correct; any `500` is a defect) and acknowledged writes lost from the
    supersede chain, followed by chain and blind-write summaries.
  - **Revocation outcome**: revoke attempt statuses, whether the first one succeeded, requests
    honoured after the revoke returned, writes dated after `revoked_at` per table (and how many ms
    after), pending proposals left, kept personal rows, and REST/MCP status of the revoked key.
- `report.json` — the same, machine-readable: `checks`, `violations`, `metrics.endpoints`,
  `metrics.phases` (throughput), `metrics.status_distribution`, `results` (`cas` with per-round
  verdicts and chain summaries, `revocation_outcome`, admin visibility, roster), `not_exercised`.
- `api-*.log`, `worker.log` — server logs. Secrets are redacted from the reports; the SQL dumps are
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
- The mock embedder used to score almost any two texts as near-duplicates (defect 7 in the findings
  report, fixed in the same change: sha512 digest, unrelated texts now score ~0.01 instead of ~0.87).
  Consolidation evidence still lives in its own repository (`/e2e/alpha-memory`) and the two G2
  scenarios run as separate consolidation passes, so runs before and after the fix stay comparable.

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
