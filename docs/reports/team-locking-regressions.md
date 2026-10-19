# Team locking regression validation

Initial reproduction base: main `cd5fafd` (PR156, head `0dd1102`, already merged).
During the follow-up, main advanced to `ca4b727` by merging PR155 and PR157.
This branch now incorporates that main and retains its principal-first lock
order, HTTP write fence, bounded transaction waits/retries, invitation advisory
lock, one-minute SKIP LOCKED usage telemetry, harness scopes and mock fix.
It does not combine the previous membership-first authorization scheme with
the newly merged principal-first scheme.

The remaining additions fence direct private-learning and draft service calls
which have no HTTP request context. Their gate locks Principal SHARE before
TeamMember SHARE, matching revocation and the HTTP fence. Embedding precedes
the gate. A committed personal insert is retired by revocation; an insert that
arrives afterwards fails. TeamDenied remains an authorization error (HTTP 403).
A PostgreSQL regression explicitly interleaves the direct gate, armed HTTP
write fence and concurrent revoke, so their interaction cannot silently invert
the principal/member lock order.

Revocation uses PostgreSQL `clock_timestamp()` after acquiring its locks rather
than host or transaction-start time. A clock-skew regression verifies the
boundary against DB-generated creation timestamps.

Project role assignment reads the project under SHARE and locks the individual
ProjectMember exclusively. This also fixes the operator path that already holds
SHARE from resolve_project. The insert-conflict fallback locks its winning row
exclusively: a three-way assignment regression had reproduced SHARE-to-UPDATE
deadlock before this fix. Upstream invitation acceptance retains its sorted
exclusive project locks; the two strategies do not upgrade a shared project.

The original six failure reproductions and the independent-review correction
are retained as historical evidence. The final suite includes upstream lock-order
tests and twelve additional PostgreSQL interleavings, including direct writes.

## Reproduction

Use a dedicated pgvector PostgreSQL server and Redis. Do not point these tests at
operator data: the fixture drops and recreates its listed tables. It requires
the host `127.0.0.1`, user `fixture`, database `brain_team_test` and the vector
extension installed by the test server administrator.

```powershell
$env:TEST_TEAM_POSTGRES_URL='postgresql+asyncpg://fixture@127.0.0.1:5459/brain_team_test'
$env:BRAIN_DISABLE_DOTENV='true'
.venv/Scripts/python -m pytest tests/test_team_locking_postgres.py tests/test_memory_cas_concurrency.py -q
.venv/Scripts/python scripts/team_memory_load_e2e.py --database-url postgresql://postgres@127.0.0.1:5459/postgres --redis-url redis://127.0.0.1:6399/0 --docker-cmd docker --pg-container brain-diag-task3-pg --users 12 --ops 50 --out ../load-final-12
```

For the 36-user, two-worker scenario run the same harness from **Linux Python**
(WSL or a Linux container), with `--users 36 --ops 50 --api-workers 2` and the
disposable services' addresses in that runtime. Windows uses the per-port API processes from PR159, avoiding the shared listener
for the transport reason below.

Supply disposable fixture credentials through your local PostgreSQL password configuration. The two
load runs must be sequential because they use the same disposable database names
and ports. Each includes onboarding, mixed REST/MCP traffic, CAS, governance,
consolidation, freshness, lease contention, provenance, revocation, restore fence,
and reads by surviving members. Reports are external to the checkout so the exact
tested commit remains clean. Inspect `checks`, `violations`, `source`, status
distribution, and API logs together; a retry must not hide a 5xx.

Mock providers do not validate real LLM/embedding APIs. Neo4j, multi-host latency,
TLS/proxy, web UI, live SSO, scheduler longevity and more than 36 principals are
outside this load harness. Lock-sensitive tests cover the listed interleavings, not every possible
administrative/OAuth/restore transaction combination.

Independent review also identified a pre-existing worker-consolidation limitation:
old L1 evidence can create a pending proposal after its author was revoked.
Approval rejects the revoked author, but this worker path is outside the new
direct/HTTP draft gate. PR157 has since added `harness:write` to standard credentials; that upstream
change is retained. The load harness still explicitly mints extra credentials
for consolidation, so its success alone does not verify every standard-client
onboarding path. The worker-generated pending-proposal limitation remains.

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

The original branch rejected native Windows with more than one worker. After
integration with PR159, the harness instead retains its per-port Windows API
processes. Linux continues to use uvicorn workers on one listener. This avoids
the unsafe Windows shared listener without reducing the requested process count.
The existing CLI single-worker launch is unchanged. An earlier diagnostic
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

## MCP context deadline follow-up

The exact `b82ec3d` Linux 36-user acceptance reported one MCP context timeout;
its 51 invariant checks passed, but the overall run failed. This is distinct
from the native Windows accept stall. A separate instrumented reproduction on
the unchanged source recorded request `6c9a40408df94c41903bd14c0e846c14`:

- Internal ASGI call started at Unix 1792313144.6317, with a 9s outer timeout.
- Repository authorization completed at 1792313150.6119 (3.092s in that stage,
  after about 2.89s of earlier authentication/dispatch).
- Retrieval took 1.942s; the complete builder returned at 1792313152.6690,
  after 2.044s. Source-delta work took 0.00063s.
- The outer bridge returned TimeoutError at 1792313153.8557 (9.240s).
  At 7.142s all 20 pool connections were checked out. PostgreSQL observations
  showed client waits, not a blocking lock chain. The reproduction is diagnostic,
  stopped after collecting the spans, and is not another acceptance PASS.

The old bridge cancelled the entire request at 9s despite a server retrieval
budget of 30s and additional assembly work. Both MCP context entry points now
send an explicit 30s total assembly budget and use a 35s transport timeout.
The API bounds the complete ephemeral builder (including post-retrieval work)
by the smaller of the caller's budget and configured context deadline. Omitted
budgets retain the existing stage behavior. Authorization remains outside the
partial-result fallback. No retries are added. The 5s difference is headroom,
not a guarantee about authorization latency; cooperative cancellation also does
not stop an already running source-delta thread.

A deadline yields an explicit `context_deadline_exceeded` partial result. The
load harness treats this and legacy context errors/retrieval deadline expiry as
violations, preventing a timeout from becoming a false PASS. Tests exercise a
real ASGI/MCP response beyond the old 9s boundary, whole-assembly cancellation,
configuration clamping, invalid/nonfinite budgets, authorization denial and
transport timeout error semantics. These are deterministic transport tests,
not evidence of live-provider behavior.

## Integration with current main

The follow-up merge retains PR159's per-port Windows API processes and removes
the superseded Windows rejection. Revocation now uses PostgreSQL wall time, so
the load harness compares its timestamps directly without a second host-clock
correction. The `db_clock_skew_ms` compatibility field is zero.
