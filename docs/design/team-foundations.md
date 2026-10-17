# Team foundations: requirement-to-code and acceptance matrix

The [architecture contract](../adr/team-collaboration.md) defines the single-team
MVP. This matrix distinguishes existing personal mechanisms from planned team
enforcement. It is updated with each implementation slice; no row is team-ready
from design or green CI alone.

| Requirement / acceptance | Current code to reuse | M1 target | Remaining stage/proof |
|---|---|---|---|
| Verified authorship; no shared-admin bypass | `brain/auth/principals.py`, API auth, `brain/team/invitations.py` | Trusted verified identity records and token-possession invitations; actor derived from live credential, not input | OIDC M5/pilot |
| Reader write/execution denial | Domain scopes, workspace capabilities | Project role/policy intersection; legacy/execution refusal | Bounded execution M2 |
| A/B isolation across API/Web/MCP | Native MCP forwards caller credential | Repository-scoped routes authorize the project per request once members exist | Cache/log/artifact adapters, unscoped routers M2–M5 |
| No run/delegation escalation | Hashed credentials; harness task ledger | Durable task/run grants and narrowed parent chain | Real executor tool capabilities M2 |
| Queued/active revocation | Credential revocation, queue lease checks | Live authorization recheck; no execution accepted | In-flight tool cancellation/checkpoints M2 |
| Concurrent participants without shared session/folder | Worktrees and task/session references | Independent run identities/provenance on one task | Actual contained environments M2, coordination M3 |
| Overlap and conflicting instructions | Impact/coupling, drift/lab modules | Contract only, not a fake detector | Coordinator, dependency/semantic scope evidence M3 |
| Stale critical-operation lease rejection | `WorkerLease`, worker DB fencing | No team operation may bypass authorization using ownership | Team leases/side-effect fencing M2/M3 |
| Exact-version approvals/current-base integration | Harness acceptance/validation artifacts | Contract only; approval/merge endpoints closed | Combined changes/reapproval/branch protection M3/M4 |
| No lost memory updates or draft promotion | Scoped stores, evidence/consolidation | Trusted-memory endpoints closed; M4 slice: member decision/rule writes become CAS proposals in a draft-isolated table, independent Reviewer approval (never the author, admins for system-global), revocation withdraws pending proposals, private personal tier ([governed memory](../MEMORY_REVIEW.md)) | Shared learnings and consolidation ungoverned; Postgres/pilot proof |
| Retry/restart idempotency | Harness idempotent events | Task/run request content binding, no raw-key replay | External-effect idempotency M2/M3 |
| Backward-compatible migration/restore | Full/Lite initialization and backup scripts | Additive tables, preserve personal rows, repeat init; disposable PostgreSQL team snapshot restore behind a reconciliation fence | Graph/secret recovery, controlled upgrade/pilot |
| Egress/model/budget separate from read | Provider adapters/control budgets | No implicit transfer grant; model surfaces closed | Enforced source consent and provider budget M2 |
| Product onboarding and board | `apps/web`, operations docs | States/fields specified in ADR; no invented UI | Invitation/task/conflict UX M5 |
| Same-project/same-task MVP | Authoritative harness ledger | Multiple isolated run records per task | Actual coordinated runs M2/M3 before M6 |

## M1 implementation: a solo Brain is a team of one

There is no team switch. `team_state()` is `solo` until the first verified
member is added (`brain team member add` or `POST /team/projects/members`) and
`team` from then on; `/health` reports it as `mode`. In the solo state every
authenticated caller, the shared `PROJECT_BRAIN_API_KEY` and keyless dev-open
access included, is the implicit administrator of every repository. In the
team state the shared key and dev-open access are refused with
`Named credential required: this Brain has team members`, and repository
access follows `ProjectRepository -> TeamProject -> member role`; an
unattached repository or an unscoped call needs a team administrator.
`BRAIN_TEAM_MODE=true` remains as a deprecated strictness alias that treats an
empty team as a team (logged once as a deprecation warning); Lite rejects it.
Trusted operators provision verified membership and named credentials, directly
or through single-use invitations (below); there is no SSO or self-registration
flow. An issuer/subject stored by a trusted operator or an invitation is not
evidence that OIDC has been integrated.

### Invitations (invited -> verified -> active -> revoked)

`brain/team/invitations.py`, `apps/api/routers/team_invitations.py`
(`POST/GET /team/invitations`, `POST /team/invitations/{id}/revoke`,
`POST /team/invitations/accept`) and `brain team invite create|list|revoke|accept`.
Additive table `team_invitations` (created with the other team tables, full
storage only). How to operate it: [inviting team members](../operations/TEAM_INVITATIONS.md).

- A team administrator (API) or the server operator (CLI) issues a `pbi_` token
  for an operator-chosen principal name, optional unverified email, project
  roles and expiry (72 h default, 720 h max). Only its SHA-256 hash is stored;
  the token is returned once with `Cache-Control: no-store`.
- Redemption is unauthenticated, throttled per client IP (10 per 5 minutes),
  row-locked and single use; the hash is compared in constant time and every
  rejected token gets one answer. It creates a human principal, a verified
  member (issuer `brain-invitation`, subject `invitation:<id>`), the project
  roles and one named credential shown once. A retry returns the same member
  without a credential. "Verified" means possession of that token, nothing more.
- Redemption fails if the inviting administrator is no longer a live admin.
- State is derived from the row and the issued credential; provenance keeps the
  inviter, acceptor and timestamps. Revoking a redeemed invitation requires an
  explicit `revoke_member`, which revokes the membership, project roles and all
  credentials of that principal; administrators cannot revoke themselves.
- Lock-out guard: the API refuses invitations in the solo state, and the first
  member must be an administrator, so the first invitation is issued and
  redeemed by the operator on the server before the shared key is retired.
- No MCP tools: one-time secrets must not land in agent transcripts.

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
- Personal surfaces authorize the repository scope through
  `apps/api/team_access.py` (`repository_authority` for body-scoped handlers,
  `repository_access(permission)` for query-scoped routers): search, ask,
  context packs, impact and diff review need `read`; decisions, rules,
  observations and lessons need `knowledge.propose` (system-global writes need
  a team administrator); job submission needs `run.request`; the web JSON,
  graph, impact, drift, coupling and remediation routers need `read` on their
  `repository_id`/`repo_path` query. The `team_*` MCP tools are served next to
  the personal tools on one server; workers and the scheduler keep running.
- The remaining personal routes follow the same rule. Harness tasks are
  authorized on the task's own repository: reads need `read`, task creation
  `task.create`, events, artifacts, validations, leases and handoffs
  `run.request`, acceptance checkpoints and finalization `task.accept`; a task
  listing or handoff query without a repository needs a team administrator.
  `GET /jobs/{job_id}`, cached impact runs and `/late-interaction/status` need
  `read` on the job's, run's or query's repository (an unreadable job or run
  answers like a missing one). Graph build, activation and rollback, drift scans
  and acknowledgements and remediation plans need `run.request`; drift review
  and remediation approvals need `task.accept`. Learnings, repository skills and
  skill outcomes need `knowledge.propose` on their repository or task (a global
  skill or unscoped learning needs an administrator). The decision and rule
  lists return global entries plus those of repositories the caller can
  `read`. Brain-wide memory (the episode and skill lists, episode and skill
  approvals, learning deletion), web reports and health, and web-login changes
  need a team administrator. The Telegram bridge refuses the shared key in team
  state and answers only for repositories its poller principal can read. A
  route that declares `repository_id` or `repo_path` is authorized only on the
  keys it declares. Intentionally open: health, readiness and version, the
  build's own quality benchmark, static action and lab-profile vocabularies,
  service and MCP status, the web-login read and verify steps of sign-in, and
  the signed merge webhook.
- In team state the dashboard forwards every data request, server-rendered
  or through its `/api/brain` proxy, with the signed-in member's key; the
  server key only probes the team state and checks sign-ins. No team executor,
  artifact download, approval, merge or provider call exists.

The SQL upgrade was rehearsed from the current main schema in a disposable
PostgreSQL database: a preexisting repository, principal and harness task kept
their exact values after repeated migration, while seven additive team tables
appeared. The full personal Python and Lite regressions run in the solo state;
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
explicitly excluded, not mocked or declared restored.

A snapshot does not contain the revocations, cancellations and policy changes
made after it, so `scripts/restore_database.py` loads every dump with team
members together with `scripts/team_restore_fence.sql` in one transaction. The
fence row (`team_access_fences`, `brain/team/restore_fence.py`) makes
`resolve_credential` honour no credential, keeps `team_state()` at `team` so the
shared key and dev-open access stay refused, denies `live_credential` and stops
OAuth issuance and refresh and invitation acceptance. `/health` reports `team_access:
pending_reconciliation` and `brain doctor` fails. A solo snapshot is not fenced.
The host `brain team` CLI is the break-glass path: `brain team reconcile` prints
the restored roster, roles, repository links, open tasks and a SHA-256 digest;
the operator replays later revocations with the other `brain team` commands and
then runs `brain team reconcile --attest <digest>`. A stale digest conflicts.
Clearing the fence revokes every restored API credential, OAuth refresh token,
run delegation and pending invitation unless `--keep-credentials` attests that the snapshot is
current; members then receive new keys. `tests/test_team_restore.py` revokes a
member and credential after the backup and proves the restored database denies
the stale credential until reconciliation, after which only the re-keyed
current member is admitted; `tests/test_team_restore_fence.py` pins the fence and
attestation semantics. A hand-run `psql` restore writes no fence. External
identities/secrets and production upgrade/restore remain unverified; this does
not make team deployment safe or ready. Operator steps:
[backup-restore runbook](../runbooks/backup-restore.md#restoring-a-team-brain).

## Evidence requirements for each slice

Record base/head SHAs, actual commands and return codes, before/after or denial
proof, unimplemented surfaces and service/platform limits. Separate SQLite unit
concurrency from real PostgreSQL concurrency; a schema test is not live restore.
Verify public author/committer/message before every push; no history rewrite,
deployment, merge or publication. Personal/Lite behavior must remain unchanged
with the gate off. Feature-on tests must show absence of fallback/data/tool effects.
