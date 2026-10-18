# Team locking regression validation

Initial reproduction base: main `cd5fafd` (PR156, head `0dd1102`, already merged).
The final branch is based on main `288fb61`, including PR151/152/153/154; the CAS fixes
are retained. The six original tests in `tests/test_team_locking_postgres.py` failed
against the initial base with real PostgreSQL: revoke/authorization deadlock, concurrent
accept deadlock, operator project lock upgrade, credential telemetry blocking,
team-wide invitation blocking, and a delayed personal insert after revocation.

The fixes use membership as the first authorization gate, including a run's human
initiator. Revocation holds that gate exclusively. Private learning inserts and
direct/HTTP draft proposals acquire the gate in their own write transaction; embedding happens
before the private write acquires it. A write either commits before revocation
and is retired/withdrawn, or observes revocation and fails. The revocation timestamp
uses PostgreSQL `clock_timestamp()` after acquiring the gate, so host clock skew
and time spent waiting cannot make already committed rows appear to be late.

Project role assignment holds a shared project lock and an exclusive membership
lock, avoiding the shared-to-exclusive project upgrade during onboarding.
The conflict fallback also takes an exclusive membership lock: independent review
identified a SHARE-to-UPDATE deadlock when three direct calls insert the same
missing membership. A PostgreSQL regression reproduces the deadlock before this
fix and verifies all three role changes and the final version afterwards.
Invitation issuance serializes duplicate names with a transaction advisory lock,
while keeping the team row shared. Credential usage telemetry updates at most
every five minutes and skips locked credentials; liveness is still read on every
authentication. A busy first use may leave the invitation's display state at
`verified` until another request records usage.

## Reproduction

Use a dedicated pgvector PostgreSQL server and Redis. Do not point these tests at
operator data: the fixture drops and recreates its listed tables. It requires
the host `127.0.0.1`, user `fixture`, database `brain_team_test` and the vector
extension installed by the test server administrator.

```powershell
$env:TEST_TEAM_POSTGRES_URL='postgresql+asyncpg://fixture:fixture@127.0.0.1:5459/brain_team_test'
$env:BRAIN_DISABLE_DOTENV='true'
.venv/Scripts/python -m pytest tests/test_team_locking_postgres.py tests/test_memory_cas_concurrency.py -q
.venv/Scripts/python scripts/team_memory_load_e2e.py --database-url postgresql://postgres:task3-disposable@127.0.0.1:5459/postgres --redis-url redis://127.0.0.1:6399/0 --docker-cmd docker --pg-container brain-fix-task3-pg --users 12 --ops 50 --out ../load-final-12
```

For the 36-user, two-worker scenario run the same harness from **Linux Python**
(WSL or a Linux container), with `--users 36 --ops 50 --api-workers 2` and the
disposable services' addresses in that runtime. Native Windows multi-worker
uvicorn is rejected for the transport reason below.

The example password belongs only to the disposable local test server. The two
load runs must be sequential because they use the same disposable database names
and ports. Each includes onboarding, mixed REST/MCP traffic, CAS, governance,
consolidation, freshness, lease contention, provenance, revocation, restore fence,
and reads by surviving members. Reports are external to the checkout so the exact
tested commit remains clean. Inspect `checks`, `violations`, `source`, status
distribution, and API logs together; a retry must not hide a 5xx.

Mock providers do not validate real LLM/embedding APIs. Neo4j, multi-host latency,
TLS/proxy, web UI, live SSO, scheduler longevity and more than 36 principals are
outside this load harness. The existing harness-write scope gap remains a product
decision. Lock-sensitive tests cover the listed interleavings, not every possible
administrative/OAuth/restore transaction combination.

Independent review also identified a pre-existing worker-consolidation limitation:
old L1 evidence can create a pending proposal after its author was revoked.
Approval rejects the revoked author, but this worker path is outside the new
direct/HTTP draft gate. The standard member and invited-admin credentials omit
`harness:write`; the load harness explicitly mints extra credentials with that
scope to exercise consolidation. This does not prove the standard onboarding
flow can submit harness tasks or observations. Neither policy gap is changed here.

## Windows multi-worker transport

The initial final-head 36-user run on `81df717` had one OAuth GET `/decisions`
ReadTimeout after 120010.4 ms. A successful repeat did not close it. A subsequent
instrumented run reproduced it on the same clean commit with correlated HTTP
client/ASGI/SQL/pool traces, PostgreSQL activity samples, worker heartbeats, Python
thread stacks and two independent external py-spy dumps (one with native frames).

Request `adc369002db04955b59b5e40b1ea3f07` was sent at Unix time
1792310039.6681974 and timed out at 1792310159.6712184. Worker 25104 admitted it
to ASGI only at 1792310159.6742134, then generated HTTP 200 in 0.141 seconds.
Throughout the stall its main thread was in `WS2_32.accept -> socket.py:295 ->
asyncio/selector_events.py:178`; the other worker and PostgreSQL were responsive,
with no blocking transaction explaining the delay. Uvicorn's supervisor pong
thread continued running, so its health check did not detect the frozen loop.

A second GET (`e1b4816aa15c44d1b6b5eff4f0aa2705`) stalled for 30.400 seconds.
One controlled fresh TCP request to a nonexistent endpoint at 1792310191.2631853
woke the worker: it admitted the pending GET at 1792310191.2728088. This establishes
the transport stall before application authorization/SQL, rather than inferring
a cause from a later successful repeat.

A minimal ASGI server with no Brain, database, OAuth or diagnostic monkeypatches
also timed out on the first fresh-connection/keep-alive transition, with two
serving Windows Selector workers and the same `accept()` stack. The attempted
Proactor alternative is rejected as a fix: one worker served while the second
failed `CreateIoCompletionPort` with `WinError 87`. A test that only counted
successful responses would have incorrectly certified it; both serving PIDs are
required. The precise Winsock shared-socket mode transition remains unknown.

The harness now fails fast for native Windows with more than one worker, before
creating databases/processes, and reports the actual Python platform/version.
This prevents misleading or stalled validation; it does not patch upstream
Winsock/uvicorn. Multi-worker acceptance must run under Linux. The existing CLI
single-worker launch and Linux behavior are unchanged. An earlier diagnostic
attempt that broke MCP imports by replacing `Popen` with a function was excluded;
the corrected reproduction retained its class interface and exercised MCP.

The bounded standalone reproduction is included for independent verification:

```text
python scripts/check_uvicorn_accept.py --workers 2 --out ../accept-default
python scripts/check_uvicorn_accept.py --workers 2 --loop asyncio:ProactorEventLoop --out ../accept-proactor
```

The first command reproduced the Windows accept stall. The second is a diagnostic
counterexample, not a supported workaround; it must fail if only one worker serves.
On Linux use the default loop. The probe writes a report and bounded stack dumps,
closes its clients and stops only the server process tree it created.
