# Team memory under many users: load e2e findings (2026-10-18)

Harness: `scripts/team_memory_load_e2e.py` ([runbook](../runbooks/team-memory-load-e2e.md)).
Code base: `main` at 411dfc2. No product code was changed for this report.

## Setup

- Real API (`uvicorn apps.api.main:app`) and real worker started from the checkout on Windows 11.
  Throwaway `pgvector/pgvector:pg16` (`max_connections=300`) and `redis:7-alpine` in WSL Docker.
  Mock LLM and embedding providers. No Neo4j (the graph reports unavailable). Generated keys only.
- Two runs are reported:
  - **12×50**: 12 principals, one API worker. 11 users ran 50 mixed operations each in the load phase
    (the revokee is held back for the revocation scenario).
  - **36×50**: 36 principals, `--api-workers 2`. 35 users ran 50 operations each.
- The default scenario parameters were used: 5 CAS rounds × 6 writers, lease contention with 8
  contenders and 15 churn cycles, revocation with 4 writer threads, revoke after 1.5 s and a 2 s tail.
- Every member sends its own `X-Forwarded-For` address from TEST-NET-2. The API trusts
  127.0.0.1 as proxy, so per-address throttles see separate clients.

## Summary

| | 12×50 | 36×50 (2 API workers) |
|---|---|---|
| Checks | 40 pass, 9 fail, 2 known gaps | 36 pass, 13 fail, 2 known gaps |
| Harness errors | none | none |
| Load phase | 605 requests in 35.4 s (17.1 rps) | 1877 requests in 73.8 s (25.4 rps) |
| Requests in the whole run | 1265 | 2966 |
| 5xx / transport errors | 34 / 3 | 32 / 4 |
| `deadlock detected` lines in the API log | 79 | 75 |

**Isolation and authority held in every run.** No reply to any member contained another project's
markers or another member's personal-tier markers. Foreign reads and writes were refused, readers
wrote nothing shared, and self-approval was refused. The consolidation gate G2 never promoted a
single principal's claims. No request sent after the revoke returned was honoured (REST and MCP all
answered 401). The pre-revocation restore did not resurrect the revoked member, and coordination
leases were never double-granted.

**What breaks are concurrency and integrity defects.** Decision versioning loses updates, two lock
order inversions deadlock (revocation and onboarding), writes in flight still land after a revoke,
and a request holding the `teams` row lock can stall unrelated requests for up to 120 s with two
API workers.

The 13 failures in the 36×50 run include 5 governance checks. They are a knock-on effect of one
stalled request (finding 5), not authority defects. The member's `POST /decisions` timed out after
120 s, so its proposal never existed, and the approve, stale-version and OAuth checks that depend
on it saw 404 or a transport error. The same governance checks passed in the 12×50 run and in the
earlier 36-user run.

## Defects, ranked

### 1. HIGH: decision CAS loses updates (lost update, broken supersede chain)

- **Where.** `brain/memory/decision_store.py::_record_decision_in` (line 66). It reads the
  canonical row through `find_canonical_decision` (line 51) without `FOR UPDATE`. It then checks
  `expected_version` in Python (`check_expected_version`, line 73) and writes an absolute version
  number. Concurrent writers that read the same version all pass the check.
- **12×50 evidence.**
  - Five rounds of six writers with the same `expected_version` produced 2, 3, 3, 2 and 2 winners
    instead of 1.
  - The chain ended at version 6 with 12 history rows instead of 5. Seven rows were unreachable
    from the canonical row, and versions 1–5 were duplicated. Seven acknowledged (200) writes are
    missing from the chain.
- **Blind writes.** Six concurrent edits without `expected_version` all returned 200. Version 9
  had 18 history rows and 10 were unreachable, so the content of 3 writers vanished from the
  history.
- **Concurrent approvals.** Four member proposals on base v1 were approved at once by different
  reviewers: 3 approved (36×50: all 4) instead of 1 approval and 3 × 409, and version 1 was
  duplicated.
- **36×50 evidence.** Rounds had 1, 1, 2, 1 and 2 winners. Two acknowledged writes were lost, and
  1 blind writer's content vanished.
- **Impact.** Approved team knowledge can be overwritten without a trace, and `version` no longer
  identifies a single row. The review queue's CAS guarantee does not hold.
- **Fix hint.** Lock the canonical row (`SELECT … FOR UPDATE`), or make the bump an atomic
  `UPDATE … WHERE id = :id AND version = :expected RETURNING`. Add a unique constraint on
  (canonical id, version) for history rows, so a lost update fails loudly.

### 2. HIGH: revoking a member who is actively writing deadlocks, and the key stays live

- **Where.**
  - `brain/team/provisioning.py::revoke_member` (line 307) locks `team_members FOR UPDATE`
    (line 315), then updates `api_credentials` in `revoke_principal_access` (line 268).
  - Every request of that member first locks `api_credentials FOR SHARE`
    (`brain/team/authorization.py::live_credential`, line 115), then `team_members FOR SHARE`
    (`human_member`, line 131).
  - These two paths take the same locks in opposite order (lock-order inversion).
- **Evidence.**
  - 12×50 (latest run): the admin's revoke returned 500 twice. Postgres aborted it as the deadlock
    victim and the transaction rolled back. The third attempt succeeded.
  - The same happened in an earlier smoke run.
  - Until a revoke succeeds, the member's key stays valid and her pending proposals are not
    withdrawn.
  - In the 36×50 run and the earlier 12×50 run, the first attempt succeeded.
- **Impact.** An admin removing a compromised or departing member gets an Internal Server Error
  exactly when the member is most active. If the admin does not retry, access is not revoked.
- **Fix hint.** Take the locks in one global order in both paths: credential before member, or
  member before credential everywhere. Alternatively, retry the revoke transaction on
  `deadlock detected` (SQLSTATE 40P01).

### 3. MEDIUM: writes in flight at revocation are persisted after `revoked_at`

- **Where.** Authorization and the write run in separate transactions. For example,
  `apps/api/routers/core_memory.py create_learning` authorizes, then the store opens its own
  session to write.
- **Evidence.**
  - 12×50 (earlier run): personal learnings 90–92 were created up to 214 ms after
    `revoked_at`.
  - 12×50 (latest run): learnings 97–98 landed after `revoked_at`.
  - 36×50: learnings 302–304 landed after `revoked_at`.
  - Proposals created in the same window ended up `withdrawn` (the proposal fence works).
  - No request *sent after* the revoke returned was honoured.
- **Impact.** Small, bounded in time, but real: a revoked member can still write in the last few
  hundred milliseconds. Today it only hits the personal tier, which only she can read.
- **Fix hint.** Re-check membership inside the write transaction: lock the member row FOR SHARE
  in the same session that inserts.

### 4. MEDIUM: concurrent invitation accepts deadlock (onboarding burst)

- **Where.** `brain/team/invitations.py::_redeem` (line 367) reads each `TeamProject` FOR SHARE
  (line 384, via `fresh`). It then calls `provisioning.assign_project_member` (line 416), which
  asks for the same row FOR UPDATE (line 421). Two accepts holding the share lock both wait to
  upgrade it, so they deadlock (lock upgrade).
- **Evidence.**
  - 12×50: 20 retries after 5xx across 7 members (2 of them on the CLI `member add` path).
  - 36×50: 23 × 500 on `POST /team/invitations/accept` (p95 3.6 s), with 23 retries across 9
    members (one member needed 6).
  - An earlier 36-user attempt saw 57 × 500 before the per-address throttle answered 429 to the
    retries.
- **Impact.** A team onboarding together (a workshop, a new team) sees random 500s on accept.
  The client must retry. The invitation is not consumed, so a retry works.
- **Fix hint.** Take `TeamProject` FOR UPDATE in `_redeem` from the start, in a stable order (sorted
  ids), or pass the locked rows into `assign_project_member`.

### 5. MEDIUM: `teams` row lock convoy, with requests stalled for up to 120 s

- **Where.** `brain/team/invitations.py::create_invitation` takes `fresh(Team, exclusive=True)`
  (line 165). While that transaction is open it also holds the admin's `api_credentials` row
  (authorization).
  - Every request's team-state check reads `teams` FOR SHARE.
  - Every request with that admin key updates `api_credentials.last_used_at`
    (`brain/auth/principals.py:137`).
  - So every such request queues behind the holder.
- **Evidence.**
  - `pg_stat_activity` during a 36-user run showed the holder `idle in transaction`
    (`ClientRead`) for more than 10 s while holding `teams` FOR UPDATE. The waiters were on
    `teams` FOR SHARE and `UPDATE api_credentials`.
  - `POST /team/invitations`: p95 12.2 s and max 13.1 s (36×50); p95 8.8 s in the earlier 36-user
    run.
  - Requests timed out after 120 s:
    - five times in one run, on invitation creation, which aborted that run's setup;
    - in two other runs, on `POST /harness/tasks`, `GET /decisions` and `POST /decisions`
      during the CAS and consolidation phases.
  - `apps/api/request_observation.py` logged `Request measurement skipped: TimeoutError` at the
    same moments: its 0.5 s telemetry write could not get a connection or lock.
- **Not isolated.** Why the holding transaction sits idle has not been found. `hash_credential`
  is a plain sha256, so key hashing is not the cause. Two candidates:
  - a handler that awaits a second pool connection while holding the first: pool size 10 +
    overflow 10 per worker, 15 s timeout;
  - a cancelled coroutine leaving a session checked out inside a transaction.
- **Impact.** Under concurrency one request can freeze unrelated requests for minutes.
- **Fix hint.**
  - Keep the `teams` FOR UPDATE window minimal and never await anything else inside it.
  - Do not write `last_used_at` on every request (throttle it, or update outside the auth
    transaction).
  - Set `idle_in_transaction_session_timeout` on the API role so a stuck holder is cut off.

### 6. LOW–MEDIUM: CAS and create races answer 500 instead of 409

- **Rule CAS.** Losing writers hit `rules_pkey` (`brain/memory/rule_store.py::rule_history_id`,
  line 32 builds the history id from the version) and get 500 "Failed to add rule". 12×50: 5 × 500
  in one round. 36×50: 4 × 500.
- **Decision create race.** Losing creators hit `uq_decisions_normalized_title`
  (`brain/database/migrations.py:363`) and get 500 "Failed to add decision" instead of 409.
  12×50: 2 × 500; 36×50: 3 × 500.
- **Impact.** The data stays consistent (the constraint holds). But clients cannot tell a conflict
  from a server fault, and the 500s pollute error monitoring.
- **Fix hint.** Map `IntegrityError` on these constraints to 409 with the current version.

### 7. LOW (test infrastructure only): mock embedder maps unrelated texts to near-duplicates — FIXED in this PR

`brain/llm/providers/mock_provider.py::_deterministic_vector` slices `digest[i*4:i*4+4]` for
`i in range(12)` over a 32-byte sha256. For `i >= 8` the slice is empty, so `idx` is always 0 and
every token adds weight to dimension 0. Any two texts then score as near-duplicates.

This makes mock-mode consolidation merge unrelated claims. The harness works around it by keeping
the consolidation evidence in its own repository.

**Fixed in this PR:** the digest is now sha512 (64 bytes, all twelve slots filled). Cosine of two
unrelated texts dropped from 0.871 to 0.009; related texts still score 0.861. Regression test:
`tests/test_llm_providers.py::test_mock_embedding_separates_unrelated_texts`. The numbers in this
report were measured before the fix; mock-mode dedup and recall scores of later runs differ.

## Known gaps (recorded, not failures)

- **Members cannot write L1 evidence in team state.** `brain/team/provisioning.py::member_scopes`
  grants `harness:read` but not `harness:write`. Member keys get 403 "lacks required scope" on
  `POST /harness/tasks` and observations, so the consolidation scenario mints separate harness keys.
- **The revoked member's personal-tier learnings are kept.** 12 rows (36×50) and 18 rows (12×50)
  remain. Nobody else can read them, but they are not retired with the membership.

## Product decision to confirm: admin reads are unscoped

An admin with no project role still reads every project's rows:

| Endpoint | 12×50 rows | 36×50 rows |
|---|---|---|
| `GET /decisions` | 19 | 17 |
| `GET /team/activity` | 33 | 24 |
| `GET /rules` | 4 | 8 |
| activity with provenance | 11 | 14 |

This may be intended, since admins administer every project, but it is not documented as such.

## Invariants that held (every clean run, both sizes)

- No cross-project or personal-tier leak in REST, MCP, search, context packs or activity.
- Foreign-project reads and writes refused (403); readers wrote nothing shared.
- Self-approval, member approval and approval by another project's reviewer refused.
- Consolidation G2 counts distinct principals. One principal's 8 events over two tasks never
  promoted. Two principals promoted, with the author left NULL and both principals in the
  evidence.
- Every item and activity event names the principal that wrote it.
- After the revoke returned, every revokee request was refused (401; 315 in 36×50, 237 in 12×50)
  and none was honoured. Pending proposals were withdrawn (9 and 17).
- Restore fence: a snapshot taken before the revocation, restored and served by a second API,
  does not bring the revoked member or her key back.
- Leases:
  - Contention: 1 × 200 and 7 × 409. Churn: 80 grants and 80 releases (36×50), with no
    overlap in time.
  - Nested-path overlap answered 409. A stale fencing token was refused, a non-holder could not
    release, and takeover bumped the token.
- Invitation-accept throttle: 429 after 10 attempts from one address, even during the burst.

## Latency (p50 / p95 / max, ms)

| Endpoint | 12×50 | 36×50, 2 workers |
|---|---|---|
| `GET /decisions` | 428 / 640 / 1375 | 860 / 2805 / 120014 |
| `POST /decisions` (member, proposal) | 667 / 1597 / 1721 | 1283 / 3718 / 120011 |
| `POST /context` | 732 / 856 / 1548 | 1350 / 2755 / 5323 |
| `POST /search` | 647 / 1731 / 1787 | 1231 / 4780 / 4943 |
| `GET /team/activity` | 742 / 956 / 1911 | 1337 / 2609 / 5723 |
| `GET /memory/proposals` | 375 / 496 / 1336 | 683 / 1836 / 4444 |
| `POST /memory/proposals/{id}/approve` | 595 / 703 / 811 | 1054 / 1739 / 2337 |
| `POST /team/invitations` | 1166 / 2328 / 2328 | 1126 / 12221 / 13084 |
| lease claim (churn) | 96 / 514 / 573 | 212 / 1361 / 1614 |

Throughput is bounded by the local single-host setup (Windows host, Postgres in WSL). The absolute
numbers are not a capacity estimate. The 120 s maxima are the stalls from finding 5.

## Harness notes

- On Windows, `uvicorn --workers` spawns children that outlive a plain `terminate()`. In one
  run they kept serving the ports with a stale database, which broke the next run. The harness now
  kills process trees (`taskkill /T` on Windows, process groups elsewhere). It also refuses to
  start when the API port is already in use.
- Invitation creation is retried after a transport error or 5xx. A pending invitation the server
  created for a timed-out attempt is revoked first. Accept retries come from a fresh address, so
  they never trip the per-address throttle, which has its own check.
- Every retry is counted and reported. A retried 5xx is still a failed check.

## Not exercised

- Real LLM and embedding providers: everything ran with the mock providers.
- The Neo4j graph: it was not started, and no scenario depends on it.
- Multi-host deployment, a reverse proxy, TLS, and network latency between API, database and
  clients.
- The web UI, OIDC/SSO and the Telegram surface.
- Long-running behaviour: retention, the scheduler, and reindexing under load.
- More than two API workers, or more than 36 principals.

## Risks

- The 36-user setup is fragile because of defects 4 and 5. Of six 36-user attempts, two completed.
  Three aborted on 120 s stalls, and one on a harness retry that tripped the accept throttle
  (since fixed). The completed run and an earlier valid 36-user run
  agree on every defect listed here.
- Lost updates (defect 1) are probabilistic. The winner counts vary between runs, but every run had
  at least two rounds with more than one winner.
- The stall root cause (defect 5) is not isolated. A fix for the lock order alone may not remove it.
