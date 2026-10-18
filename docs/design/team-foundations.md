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
unattached repository or an unscoped call needs a team administrator. Such a
call grants the administrator full authority over every repository, project
repositories included, so a team administrator without a project role reads
every project's memory through unscoped calls and unfiltered listings, but is
refused on calls scoped to a project repository (see the ADR's
[team administrator reads](../adr/team-collaboration.md#team-administrator-reads)).
`BRAIN_TEAM_MODE=true` remains as a deprecated strictness alias that treats an
empty team as a team (logged once as a deprecation warning); Lite rejects it.
Trusted operators provision verified membership and named credentials, directly
or through single-use invitations (below); there is no self-registration flow.
An issuer/subject stored by a trusted operator or a token-only invitation is not
evidence of an identity provider. Optional OIDC single sign-on (off by default,
[TEAM_SSO.md](../operations/TEAM_SSO.md)) accepts an emailed invitation only for
the IdP account that proves the invited email, records the IdP issuer/subject
as the member's verification, and maps a bound IdP account to an active member
for dashboard sign-in; it never provisions members.

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

## M2 bounded execution: first slice

Operator reference: [bounded team execution](../operations/TEAM_EXECUTION.md).
Execution is off unless `BRAIN_EXECUTION_ENABLED` and `BRAIN_EXECUTION_ROOT` are
set, and then still needs the project policy's `execute`, a Contributor or Reviewer
role and a separate per-member grant (`team_execution_grants`) that no role implies.
`authorize()` is unchanged; `brain/team/execution.py` layers the execution gates on
top of it and re-evaluates all of them before each tool operation and each ledger
write.

| Requirement | Code | Evidence (`tests/test_team_execution.py`) |
|---|---|---|
| Denied by default | `ExecutionPolicy.disabled_reason`, `authorize_execution` | `test_execution_is_denied_by_default`, `test_enabled_without_root_is_still_denied` |
| Separate per-member grant | `TeamExecutionGrant`, `set_grant`, provisioning revocation hooks | `test_denied_without_grant_and_grants_follow_roles`, `test_member_revocation_revokes_execution_grants` |
| Capabilities as data, children narrow | `AgentRunExecution` (tools, secret refs, egress, budget), `_ceiling`, `_within` | `test_capabilities_outside_the_allowlists_are_denied`, `test_child_runs_narrow_to_parent_capabilities` |
| Egress default none | backend `enforces_egress`, operator allowlist | egress cases in the capability tests |
| Clone per run, subprocess runner | `brain/execution/bounded.py` (`SubprocessBackend`, `ExecutorBackend`) | `test_execution_records_evidence_and_never_stores_secrets` |
| Revocation checkpoints | `ExecutionRunner.checkpoint`, step stop probe | `test_revocation_between_steps_stops_the_next_step`, `test_revocation_mid_step_terminates_the_process`, `test_cancel_before_start_and_during_a_run` |
| Lease, fencing, crash recovery | `acquire_lease`, `_fence`, `recover_orphaned_runs` | `test_expired_lease_owner_cannot_commit`, `test_restart_fences_orphaned_runs_and_never_resumes_them` |
| Idempotency bound to content and run | request digest over run, task, backend and canonical request | `test_idempotency_is_bound_to_content_and_run` |
| Secrets never in records | env-only injection, redaction | `test_execution_records_evidence_and_never_stores_secrets` |
| One status machine | `run.execution.*` events in `harness.agent_task_events` | the same test checks the event sequence |
| API behind every gate | `apps/api/routers/team.py` execution routes | `test_http_execution_routes_sit_behind_every_gate` |

The subprocess backend is not containment, so egress requests are refused. The
container backend, egress enforcement, environments, provider consent and LLM
budgets, agent-driven steps, artifacts, external-effect idempotency, doctor
reporting, PostgreSQL concurrency tests, UI/MCP surfaces and resuming fenced runs
remain M2 work.

## Workspace registry and team membership: one authority

Before this change the two models answered different questions on different routes
and never consulted each other:

- The workspace-registry routers (freshness, lab, experiments, ledger, operations,
  workspace record reads) authorized membership through `registry_authority`, then
  checked the record's `disabled` flag and capabilities themselves. Ledger reads and
  operations' vector repair checked no registry trust at all.
- Every personal route (search, context, graph, impact, jobs, memory listings, the
  Telegram bridge, worker re-authorization) consulted only membership. A repository
  that the registry had disabled, or registered as untrusted external code, stayed
  fully usable by its project's members and by team administrators.
- Registering a repository in the registry (admin-only in team state) linked it to no
  project, and attaching a repository to a project ignored the registry. A registered,
  trusted repository could be usable by nobody but administrators, and a disabled one
  by every member of the project listing it.

Now `brain.team.authorization.authorize_repository` decides both, in team state only:

| Question | Owner | Where |
|---|---|---|
| Who may use the repository | Team membership (project role, project policy, credential scopes); unattached repositories and unscoped calls (every repository): team administrators | `authorize`, `team_administrator` |
| What anyone may do on it | Workspace registry trust: no record = no restriction; `untrusted_external` = read only; disabled, archived or trust level `disabled` = nothing, for everyone including administrators; unknown trust level or unreadable registry = nothing | `registry_trust_ceiling`, `brain/workspace/trust.py` |

Membership is evaluated first, so a caller without access is told "Project access
denied" and learns nothing about the registry. The returned authority's permissions
are intersected with the trust ceiling. `repo_path` lets registry routes pass a
record's canonical root even when it is not indexed. `readable_repository_ids` drops
repositories the registry no longer trusts for reading from member listings;
administrators still list every repository but are refused per-repository access.
Route-level checks stay where they were (the `disabled` 409 and capability 403 in the
registry routers): solo state still relies on them, so none became redundant.

In team state `POST /workspace/repositories` needs a team administrator who
administers the target project and links the repository to it in the same database
transaction as the registry write: `project_id` names the project, or the repository
must already be attached to one. The repository must be indexed first (409 otherwise);
a repository attached to another project is refused (409) rather than moved; no
`project_id` and no existing link is refused (422). The offline `brain workspace` CLI
(`python -m brain.workspace.cli`) needs `--project` in team state and links the same
way, in one transaction.

In team state the registry is one PostgreSQL table, `workspace_registry_entries` (with
its audit trail in `workspace_registry_events`; `brain/team/registry.py`), read by the
API, the worker's per-job re-authorization and the CLI alike. Each process's legacy
file is imported once while the table is empty and read, with a warning, only until
then ([upgrade note](../operations/TEAM_DEPLOY.md#workspace-registry-file-to-table)).
Every team-state registry change (register + link, disable, re-enable) commits with its
`registry.*` row in the team activity feed. A team administrator can re-enable a
disabled entry (`POST /workspace/repositories/{id}/enable`, `brain workspace
repositories enable`; the earlier trust level unless one is given) and open it by id
(`registry.inspected`).

Solo state is unchanged: `authorize_repository` returns before reading the registry,
registration ignores `project_id`, and the registry stays the file. The link is the
existing `project_repositories` row, matched to the registry record through
`repositories.path`.

| Requirement | Evidence |
|---|---|
| Agreement matrix: trust (none, trusted, fixture, untrusted external, disabled, withdrawn) x caller (member, non-member, administrator member, administrator non-member, unattached) x solo/team | `tests/test_team_workspace_authority.py::test_team_agreement_matrix`, `test_solo_ignores_the_registry` |
| Disabled repository denied even though a project lists it; administrators included | `test_disabled_repository_is_denied_to_its_project_and_to_administrators`, `test_personal_routes_answer_403_on_a_disabled_repository` |
| Unindexed registry record and unreadable registry | `test_registry_path_that_is_not_indexed_carries_its_trust`, `test_unreadable_registry_fails_closed_in_team_state_only` |
| Member listings | `test_member_listings_drop_repositories_the_registry_does_not_trust`, `test_member_listing_hides_a_disabled_repository` |
| Registration links to a project or refuses | the `test_team_registration_*`, `test_duplicate_registration_rolls_back_the_link` and `test_solo_registration_is_unchanged` tests |
| Trust lookup by path spelling | `tests/test_workspace_registry.py::TestRegistryTrust` |

Known limits: the registry is a file under each process's working directory
(`.brain/workspace`, not on a volume), so the worker container re-authorizes against
its own, usually empty, registry; the registry write and the database commit are not
one atomic step (a commit that fails after the registry write leaves a record with no
project link, which grants nobody but administrators); administrators cannot read a disabled record by id
(`GET /workspace/repositories/{id}`), only in the listing.

## Evidence requirements for each slice

Record base/head SHAs, actual commands and return codes, before/after or denial
proof, unimplemented surfaces and service/platform limits. Separate SQLite unit
concurrency from real PostgreSQL concurrency; a schema test is not live restore.
Verify public author/committer/message before every push; no history rewrite,
deployment, merge or publication. Personal/Lite behavior must remain unchanged
with the gate off. Feature-on tests must show absence of fallback/data/tool effects.
