# Team foundations: requirement-to-code and acceptance matrix

The [architecture contract](../adr/team-collaboration.md) defines the single-team
MVP. This matrix distinguishes existing personal mechanisms from planned team
enforcement. It is updated with each implementation slice; no row is team-ready
from design or green CI alone.

| Requirement / acceptance | Current code to reuse | M1 target | Remaining stage/proof |
|---|---|---|---|
| Verified authorship; no shared-admin bypass | `brain/auth/principals.py`, API auth | Trusted verified identity records; actor derived from live credential, not input | Real invitation/OIDC M5/pilot |
| Reader write/execution denial | Domain scopes, workspace capabilities | Project role/policy intersection; legacy/execution refusal | Bounded execution M2 |
| A/B isolation across API/Web/MCP | Native MCP forwards caller credential | Project-bound task APIs/tools; unadapted surfaces denied | Retrieval/cache/log/artifact adapters M2–M5 |
| No run/delegation escalation | Hashed credentials; harness task ledger | Durable task/run grants and narrowed parent chain | Real executor tool capabilities M2 |
| Queued/active revocation | Credential revocation, queue lease checks | Live authorization recheck; no execution accepted | In-flight tool cancellation/checkpoints M2 |
| Concurrent participants without shared session/folder | Worktrees and task/session references | Independent run identities/provenance on one task | Actual contained environments M2, coordination M3 |
| Overlap and conflicting instructions | Impact/coupling, drift/lab modules | Contract only, not a fake detector | Coordinator, dependency/semantic scope evidence M3 |
| Stale critical-operation lease rejection | `WorkerLease`, worker DB fencing | No team operation may bypass authorization using ownership | Team leases/side-effect fencing M2/M3 |
| Exact-version approvals/current-base integration | Harness acceptance/validation artifacts | Contract only; approval/merge endpoints closed | Combined changes/reapproval/branch protection M3/M4 |
| No lost memory updates or draft promotion | Scoped stores, evidence/consolidation | Trusted-memory endpoints closed | CAS proposals/history/independent approval M4 |
| Retry/restart idempotency | Harness idempotent events | Task/run request content binding, no raw-key replay | External-effect idempotency M2/M3 |
| Backward-compatible migration/restore | Full/Lite initialization and backup scripts | Additive tables, preserve personal rows, repeat init; disposable PostgreSQL team snapshot restore | Post-snapshot revocation reconciliation, graph/secret recovery, controlled upgrade/pilot |
| Egress/model/budget separate from read | Provider adapters/control budgets | No implicit transfer grant; model surfaces closed | Enforced source consent and provider budget M2 |
| Product onboarding and board | `apps/web`, operations docs | States/fields specified in ADR; no invented UI | Invitation/task/conflict UX M5 |
| Same-project/same-task MVP | Authoritative harness ledger | Multiple isolated run records per task | Actual coordinated runs M2/M3 before M6 |

## Experimental M1 implementation in the foundation change

`BRAIN_TEAM_MODE` defaults to `false`. Full-mode installations may explicitly
enable the experimental boundary; Lite rejects it. Trusted operators must
provision verified membership and named credentials in the database before use;
there is no public invitation, SSO, or self-registration flow. An issuer/subject
stored by a trusted operator is not evidence that OIDC has been integrated.

- `brain/team/models.py` adds single-team/project/member/repository/task bindings
  and per-run provenance in the existing database and harness schema. The task
  ledger remains `harness.agent_tasks`; older rows are not silently adopted.
- `brain/team/authorization.py` reloads the credential, human membership,
  project policy, task authority and every parent run under transaction locks.
  Roles and executor capabilities only narrow access; revocation and accepted
  task cancellation fence later run requests. Run credentials are stored hashed.
- Project task listing/creation and run **requests** are available at the team
  API and native MCP transport. Idempotency keys bind the request content;
  retries cannot recover a raw run credential. `declared_base_sha` and `branch`
  are caller declarations, not verified Git ancestry or checked-out worktrees.
- Personal API/web, remote/local personal MCP, unscoped worker/scheduler/jobs,
  search, context, memory, logs, downloads, notifications and provider routes
  are **not adapted**: team mode refuses them. Its `/health` returns only a
  non-project-specific experimental marker, not legacy worker/queue details.
  Personal retrieval warmup and remote-release validation also stay disabled
  during API startup, even when those settings are inherited.
  No team executor, artifact download, approval, merge or provider call exists.

The SQL upgrade was rehearsed from the current main schema in a disposable
PostgreSQL database: a preexisting repository, principal and harness task kept
their exact values after repeated migration, while seven additive team tables
appeared. The full personal Python and Lite regressions run with the flag off;
PostgreSQL-only concurrency cases exercise simultaneous task/run retry and
membership-version updates. This is migration preservation evidence, not a
production restore, team deployment or working collaboration execution proof.

`tests/test_team_restore.py` exercises the real PostgreSQL backup/restore scripts
under a non-superuser fixture role with database-creation permission. It compares
every column in all twelve foundation/ledger tables before and after restore,
including a legacy unbound task, credential hashes, revocations, permission
versions, request digests and parent-run provenance. Restored authorization must
still deny reader writes, cross-project access, revoked credentials, delegation
expansion, execution and cancelled-task run requests; stale policy/membership
updates must conflict. Retries produce no new records or raw-key replay, while
new requests verify restored sequence continuity. Wrong target confirmation and
checksum corruption must leave the restored target unchanged.

The test is opt-in through `TEST_TEAM_POSTGRES_URL`, restricted to the disposable
loopback `brain_team_test` database and `fixture` role; its target is exclusively
`brain_team_restore_test`. CI requires it alongside PostgreSQL authority tests.
Local PostgreSQL clients must support the fixture server version. Neo4j is
explicitly excluded, not mocked or declared restored. Recovery preserves the
backup snapshot only: revocations, cancellations and policy changes after that
snapshot require separate reconciliation before reopening access. That recovery
procedure, external identities/secrets and production upgrade/restore remain
unimplemented/unverified; this test does not make team deployment safe or ready.

## Evidence requirements for each slice

Record base/head SHAs, actual commands and return codes, before/after or denial
proof, unimplemented surfaces and service/platform limits. Separate SQLite unit
concurrency from real PostgreSQL concurrency; a schema test is not live restore.
Verify public author/committer/message before every push; no history rewrite,
deployment, merge or publication. Personal/Lite behavior must remain unchanged
with the gate off. Feature-on tests must show absence of fallback/data/tool effects.
