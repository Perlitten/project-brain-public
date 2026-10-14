# ADR: isolated single-team collaboration

Status: target architecture; M0 contract, not a team-readiness claim.

Requirements source: the owner's 2026-10-14 summary of [Team Brain](https://app.notion.com/p/34533f3b6a82815bbec8d9b43088e066). Notion is design input, not implemented-feature evidence.

## Boundary and inventory

One isolated installation serves one team, multiple projects/repositories/members.
Independent-organization multi-tenant SaaS is outside this MVP. Personal/full and
personal/Lite remain supported with team mode off; no implicit membership,
ownership, migration-to-global or access widening. Team mode is experimental and
default-off; incomplete team surfaces must refuse access rather than use personal
fallbacks. Team members do not get control-host shell/database accounts.

| Existing mechanism | Reuse / missing contract |
|---|---|
| `brain/auth/principals.py`, `models.Principal/ApiCredential` | Hashed/revocable named credentials; current domain scopes, `org_id`, kind/name and legacy key do NOT establish verified humans/project isolation. |
| `apps/api/auth.py`, `apps/api/mcp_http.py` | Attributed API/native MCP; personal dev-open/shared key must not grant team authority. Native MCP forwards caller credentials to API. |
| `brain/database/models.py::Repository` | Durable integer primary key; paths are locations, not identity. Do not derive team repository identity from a path hash. |
| `brain/database/harness_models.py`, `brain/memory/harness_store.py` | Authoritative durable task/event/artifact/validation/lease ledger. Extend its scope/run provenance, not create another orchestration database. |
| `brain/workspace/{registry_store,capabilities,path_sandbox}.py` | Repository trust/tool capabilities and filesystem checks; not human project membership. Worktree isolation is not security containment. |
| `brain/workers/{queue,db_fencing,worker,tasks}.py` | Lease/DB fence/retry mechanisms; ownership does not imply project permission. Legacy dispatch must be blocked in team mode until scoped execution exists. |
| `brain/memory/{rule_store,decision_store,consolidation,validated_evidence}.py` | Scoped memory, evidence and promotion machinery; not yet project-authorized governed normative memory. |
| `apps/web`, API web routers, MCP tools/resources | Personal workflows/cache/artifacts are not team-authorized; unadapted paths must be blocked, not expose global data. |
| `scripts/{backup_runner,restore_database,restore_verification_drill}.py` | Explicit full backup/PG restore. Neo4j rebuild requires retained revision; Redis queue is not in this backup. Artifact validation is not live-restore proof. |

## Entities and authoritative identity

Hierarchy: team -> project -> stable Repository.id -> task/subtask -> run ->
artifact/result/knowledge. Team/project IDs are durable, not client actor names.
Existing personal rows stay unscoped; a separate explicit project binding adopts
a repository/task, never an automatic inferred membership. Initially each
repository is bound to one project; widening requires explicit review.

Human credentials authenticate a server-owned Principal linked to a verified
team identity. Verification must have issuer/subject/provenance; `kind=human`
alone is insufficient. Initial provisioning is an offline trusted operator
action, NOT self-registration or a claim of deployed OIDC. Invitation/SSO is M5.
Run credentials identify a particular run and store only hashes. Git/provider
secrets stay server-held and are not copied into run records or client payloads.

Tasks remain `harness.agent_tasks`. Add project/repository/author binding; add run
provenance in the same ledger schema, not a second status machine. Each run has
its own identity, parent delegation, task, initiator, base/head SHA, branch and
working-copy location. Branch/path are not identities or access tokens. No
executor is simulated: M1 records authority/provenance only; execution is denied.

## Permission matrix

| Action | Reader | Contributor | Reviewer | Team administrator |
|---|---|---|---|---|
| Project read | Yes | Yes | Yes | Only with project membership |
| Task creation / run request | No | Eligible | Eligible | Only with project role |
| Knowledge proposal | No | Eligible | Eligible | Only with project role |
| Normative approval / task acceptance | No | No | Eligible, independent author | Not implied by administration |
| Execution | No | Separate explicit grant | Separate explicit grant | Not implied |
| Merge | No | Separate exact-version approval | Separate exact-version approval | Not implied |
| Membership/policy administration | No | No | No | Separate team-admin permission |
| External-provider transfer | No implicit grant | No implicit grant | No implicit grant | No implicit grant |

Eligibility is not an enabled feature. Effective run authority is the intersection
of live initiator credential/membership, project policy, task permissions,
delegation chain and executor capabilities. Each child narrows authority. No
lease/task ownership, wildcard credential, client actor, admin flag or repository
read adds privileges. Missing identity/policy/scope/provenance denies access.

Central authorization runs BEFORE data retrieval/model transfer and is reused by
API/Web, native/remote MCP and jobs. No global retrieval-then-filter design.
Search/graph/memory/context/cache/artifact/log/notification adapters must authorize
the same project/source scope; until adapted, block them in team mode. Derived
knowledge inherits source restrictions; broader publication requires independent
approval. Egress consent, provider availability and budget are separate gates.

## Revocation, concurrency and approval

Re-resolve live credential, member, project and every parent run before subsequent
reads/side effects; cached request authority is not durable permission. Revoked
initiator/parent invalidates descendants. M1 has no execution: queued and active
legacy work is refused. M2 checkpoints before each bounded tool operation and
transactional write; already-sent network effects cannot be recalled. Revocation
must request cancellation, refuse subsequent effects and explicitly report any
in-flight operation, not promise retroactive cancellation.

Policy/membership/knowledge changes use expected versions and compare-and-swap;
stale reviewers get conflict, never last-write-wins. No normative self-approval.
Proposal -> review -> approved knowledge preserves provenance/history and keeps
draft branch facts separate from approved project knowledge. Paraphrases are not
independent evidence. Readers may create temporary context/audit, not trusted rules.

Same-task collaboration is IN the first team MVP: coordinator + subtasks,
dependencies and independent isolated runs. Declared and actual scope is checked
at launch, during changes and before integration. Semantic overlap flags risk,
not a universal detector: same-file work can be independent; separate files can
conflict through APIs/schema/units. Conflicting instructions require the responsible
person's explicit versioned resolution. Local unregistered edits are invisible.

Risky exclusive operations use bounded leases, fencing and version checks; expired
owners cannot commit. Idempotency binds to request content and stable run/task
identity, survives restart, and conflicts on changed content. Integration validates
the combined patch on current target head plus accepted predecessors. Human
approval binds exact artifact checksum/commit/base/version; meaningful changes
require reapproval. Branch protection remains authoritative; proposed fixes/PRs
never authorize merge. Reuse existing evidence/lease mechanisms where suitable.

## Product flow, packaging and migration

Flow: create isolated team -> connect repositories/models/executors -> configure
policy/budget -> invite verified members -> permitted projects/tasks -> evidence,
coordination and knowledge review. Web and IDE/MCP use the same server authorization.
Board contract: participants, dependency graph, task/run version, base/head,
evidence, budget, required actor and next action: parallel / needs coordination /
waiting dependency / recheck / ready to integrate. Invitation states are invited,
verified, active, revoked; conflicts name the responsible resolver and exact version.
These are UX requirements, not screenshots of implemented features.

Supported cloud target: one team's self-hosted Linux Docker Compose deployment
behind HTTPS, one API instance (stateful MCP), private PG/Neo4j/Redis networks,
separate bounded executor containers and server-held secret mounts. No paid
provisioning now. Team members never receive primary DB/admin secrets or host mounts.
Quiesce personal workers before enabling team mode; all services use the same
configuration. Doctor must eventually report auth, pending revocations, leases,
provider/egress/budget state, source revision and backup/recovery status.

M1 migration is additive tables: no reinterpretation/deletion of old personal rows,
no automatic adoption or backfill grants. Explicit operator provisioning/binding
is required. Team mode is not supported in Lite; reject that combination while
keeping personal Lite unchanged. Back up before updates; restore additive ledger
and identity rows with PostgreSQL, retain source revision and rebuild graph.
Demonstrate legacy-row preservation, repeat initialization and disposable restore;
green ADR/CI is not team readiness.

## Stages and dependencies

- **M0:** this inventory/contract, permission/revocation/conflict semantics and coverage matrix.
- **M1:** durable scope, verified-identity provenance, task/run binding and centralized
  fail-closed enforcement; denial/revocation/CAS/idempotency/migration tests. Unadapted
  surfaces and all execution remain gated. No fake autonomy executor.
- **M2 after M1:** bounded real executors, run tools/secrets/egress/budget capabilities,
  revocation checkpoints, environments, timeout/cancel/crash fencing.
- **M3 after M1/M2:** same-task subtasks/dependencies/coordinator, semantic overlap,
  instruction-resolution versions, critical leases and current-base integration queue.
- **M4 after M1 (execution evidence also needs M2):** governed versioned memory,
  exact-source provenance, independent review, draft isolation and optimistic updates.
- **M5 after M1, integrated with M2–M4:** invitation/board/conflict UX, one-team cloud
  packaging, update/backup/restore diagnostics. Not an independent tenancy system.
- **M6 after M1–M5:** real pilot with 3–5 participants and two projects, deliberate
  overlaps/revocations/crashes. No invented date/results; not authorized to provision now.

See [coverage and acceptance matrix](../design/team-foundations.md).
