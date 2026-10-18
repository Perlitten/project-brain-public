# Team mode: current status

Snapshot: 2026-10-18, `main` (ca4b727) after PRs #132–#157 (invitation UI,
shared throttle, `/team/me` and activity feed, origin poll, bounded execution,
governed memory, team e2e check, OIDC SSO, doctor and cloud packaging, one
repository authority, coordination, board, hardening, local principal, shared
workspace registry; then wave 4: a verified HTTPS front (#151), the M3
leftovers (#152), offline CLIs on the shared registry and review notices for
every reviewer (#153), the M2 leftovers (#154), a multi-user memory load e2e
(#155), atomic decision/rule CAS (#156) and the team lock-order and write-fence
fixes (#157)). Update this file in
the same PR as any team-mode change; it is the one place that answers "what
stage is team mode at". The contract is the [ADR](../adr/team-collaboration.md);
the per-requirement evidence is the [foundations matrix](team-foundations.md).

## In one paragraph

**M1** (identity, membership, fail-closed authorization, invitations with a
dashboard UI, optional OIDC SSO, one repository authority) is implemented on
`main`: a solo Brain is a team of one, and adding the first verified member
switches repository-scoped API, MCP and dashboard requests to per-project
authorization; #157 made revocation deadlock-free and fenced in-flight writes.
**M2** (bounded execution, off by default) now has provider consent, an LLM
budget, artifacts and a real-docker CI job; only agent-chosen steps are
deferred. **M3** (coordination, board, conflict view) has admin force-release,
graph-dependency overlap and a combined patch at integration. The **M4** write
paths (decisions, rules, learnings, skills, consolidation) are governed by
proposals and review, with atomic CAS (#156) and notices to every reviewer
(#153). **M5** (doctor, cloud packaging) has an HTTPS front checked end to end
(#151). Team mode is **not deployed** (production runs c13d056) and has **not
been tried with real people**; green CI, the scripted
[team e2e check](../runbooks/team-e2e-check.md) and the
[multi-user memory load test](#multi-user-memory-load-test-2026-10-18) are the
only evidence so far, and the ADR says plainly that green CI is not team
readiness.

## Stages

| Stage | Scope | Status |
|---|---|---|
| M0 | Contract, permission/revocation semantics, coverage matrix | Done |
| M1 | Verified identity, membership, centralized authorization, provisioning, invitations, restore fence | Implemented, including the invitation UI (#132), OIDC SSO (#139, #144), one repository authority (#141, #149) and concurrency hardening: one global lock order, deadlock-free revoke, write fence, bounded team transactions (#157); not deployed |
| M2 | Bounded real executors, tool/secret/egress/budget capabilities, revocation checkpoints | Off by default: grants, leases with fencing, subprocess and container backends, execution environments, egress proxy (#136, #143, #147); LLM provider consent and budget, execution artifacts and a real-docker CI job (#154). Deferred: agent-chosen steps ([rationale](../operations/TEAM_EXECUTION.md#why-agent-chosen-steps-are-deferred)) |
| M3 | Subtasks, coordinator, overlap detection, integration queue | Subtask DAG, declared-scope and changed-files overlap warnings, instruction versions, fenced leases, integration queue, MCP coordination writes (#142, #146, #147); admin force-release (API, MCP, CLI), graph-dependency overlap warnings and a real combined patch at integration (#152). Coordination does not merge or push |
| M4 | Governed versioned memory, independent review | Write paths governed: provenance and CAS (#129); member decisions and rules become proposals with a review queue and a personal learnings tier (#137); member learnings and skills, consolidation and G4 episodes go through proposals; nobody approves their own (#145, #148); review notices to every reviewer (#153); atomic decision/rule CAS and retired learnings of revoked members (#156) |
| M5 | Invitation/board/conflict UX, cloud packaging, upgrade/restore diagnostics | Invitation UI (#132), `/team/me` and activity feed (#134), doctor and cloud packaging (#140), board and conflict view (#146), the Caddy/TLS front checked end to end in CI with four defects fixed (#151). Not yet run on a live host |
| M6 | Pilot with 3–5 people on two projects | Not started; needs real people |

## Public label

Team mode may be announced only as an **experimental preview: `main` only, not
in v0.9.2**, never as generally available. The operator path (init, attach a
repository, invite, dashboard sign-in, MCP, revoke) and the user-facing known
limitations are in the [team mode walkthrough](../TEAM.md); keep its
limitations in step with the known gaps below. The security reporting scope
is in [SECURITY.md](../../SECURITY.md#team-mode-experimental-preview).

## Shipped in M1

| What | PR |
|---|---|
| Gated project authority, durable task/run foundations | [#99](https://github.com/Perlitten/project-brain-public/pull/99) |
| `brain team` CLI and `team:admin` API; OAuth clients bound to members; principal-attributed memory writes | [#111](https://github.com/Perlitten/project-brain-public/pull/111) |
| Unified model: no team switch, `team_state()` = `solo` / `team`, one MCP server | [#112](https://github.com/Perlitten/project-brain-public/pull/112) |
| Repository listing, diff-review results and context packs scoped by membership | [#113](https://github.com/Perlitten/project-brain-public/pull/113), [#118](https://github.com/Perlitten/project-brain-public/pull/118) |
| Registry and global routers authorized per repository; administrative routers admin-only | [#114](https://github.com/Perlitten/project-brain-public/pull/114), [#116](https://github.com/Perlitten/project-brain-public/pull/116) |
| Queued jobs record their initiator and are re-authorized before execution | [#115](https://github.com/Perlitten/project-brain-public/pull/115), [#117](https://github.com/Perlitten/project-brain-public/pull/117) |
| Single-use invitations (invited → verified → active → revoked), API and CLI | [#119](https://github.com/Perlitten/project-brain-public/pull/119) |
| Remaining personal routes scoped in team state | [#120](https://github.com/Perlitten/project-brain-public/pull/120) |
| Dashboard members sign in with their own key; admin Team page (add, role, revoke) | [#121](https://github.com/Perlitten/project-brain-public/pull/121) |
| A team snapshot restored with `scripts/restore_database.py` stays fenced until `brain team reconcile --attest`; plain `psql` does not add the fence ([runbook](../runbooks/backup-restore.md#restoring-a-team-brain)) | [#122](https://github.com/Perlitten/project-brain-public/pull/122) |
| CLI and invitation keys carry the same scopes as dashboard keys; members read global decisions and rules plus those of repositories they can read (`/decisions`, `/rules`, MCP `brain://decisions`, `brain://rules`) | [#124](https://github.com/Perlitten/project-brain-public/pull/124) |
| Member keys get `jobs:write` (queueing still needs Contributor or Reviewer on the repository); team-admin keys also get `setup:write` and `operations:write`, so "Run now", setup and retries work from the dashboard. Keys minted before this change lack these scopes and must be re-issued | [#124](https://github.com/Perlitten/project-brain-public/pull/124) |
| Dashboard for non-admin members: navigation and pages limited to projects they have a role on; Settings, Setup, Quality, Logs, Runs, Reranker, Admin and reports show "Team admins only"; no setup links | [#124](https://github.com/Perlitten/project-brain-public/pull/124) |
| Dashboard shows "unavailable" instead of a false "none" when Brain does not answer (jobs, findings, memory, packs, runs, MCP tools, graph, team roles) | [#124](https://github.com/Perlitten/project-brain-public/pull/124) |
| Revocation hardening (below) | [#127](https://github.com/Perlitten/project-brain-public/pull/127) |
| Dashboard invitation UI: the Team page issues, tracks, revokes and redeems invitations; the public `/invite` page keeps the token in the URL fragment, posts same-origin only, shows the key once and signs the invitee in. Invitations are still not emailed | [#132](https://github.com/Perlitten/project-brain-public/pull/132) |
| Invitation acceptance throttle shared through Redis (10 attempts per IP per 5 minutes, `Retry-After`, per-process fallback if Redis fails); repository paths outside the allowed roots answer 400 instead of 500 on `/late-interaction/status` and the insights, impact, context, related and features routes (`POST /index`: 403 → 400) | [#133](https://github.com/Perlitten/project-brain-public/pull/133) |
| `GET /team/me` and `GET /team/activity` (MCP `team_me`, `team_activity`, dashboard pages) over an append-only `team_activity_events` table; history starts with this release | [#134](https://github.com/Perlitten/project-brain-public/pull/134) |
| Scripted team e2e check (`scripts/team_e2e_check.py`, opt-in "Team e2e" workflow, [runbook](../runbooks/team-e2e-check.md)); fixed commit ordering and legacy authorship backfill on edit | [#138](https://github.com/Perlitten/project-brain-public/pull/138) |
| Optional OIDC SSO (PKCE, state, nonce, browser binding), `team_identities`, operator `sso-bind`; one IdP per Brain, no auto-provisioning ([TEAM_SSO.md](../operations/TEAM_SSO.md)) | [#139](https://github.com/Perlitten/project-brain-public/pull/139) |
| One repository authority: `authorize_repository` is capped by the workspace registry trust ceiling (`untrusted_external` read-only, disabled/archived denied to everyone, a corrupt registry fails closed); registering a repository in team state needs an administrator and a `project_id` | [#141](https://github.com/Perlitten/project-brain-public/pull/141) |
| Invited members can approve team OAuth clients and their approvals are revoked with them; one live SSO session credential per binding, `POST /team/oidc/logout`, SSO on `/invite`; `BRAIN_TRUSTED_PROXY_IPS` for the client IP behind a proxy | [#144](https://github.com/Perlitten/project-brain-public/pull/144) |
| In team state the workspace registry lives in PostgreSQL (`workspace_registry_entries`, `workspace_registry_events`), so the API, the worker and the offline workspace CLI share one trust; one-time import of the API's registry file, atomic registration with the project link, re-enable, admin read of a disabled entry, `registry.*` activity. Solo state keeps the file | [#149](https://github.com/Perlitten/project-brain-public/pull/149) |
| The offline CLIs (control plane, workspace, experiments, freshness, lab, portfolio) resolve the registry through `brain/team/cli_registry.py`: the PostgreSQL table in team state, the file in solo state; once a CLI has seen team state, an unreachable database fails with exit code 2 instead of falling back to the file | [#153](https://github.com/Perlitten/project-brain-public/pull/153) |
| Concurrency hardening (below) | [#157](https://github.com/Perlitten/project-brain-public/pull/157) |

### Revocation hardening

- **Revoking a member revokes everything acting for it**, in the same
  transaction, on every path (`DELETE /team/projects/members/{id}`,
  `brain team member revoke`, invitation revoke with `revoke_member`): all of
  the principal's credentials (named keys and OAuth access tokens), its OAuth
  refresh tokens, and the runs it initiated together with their run
  credentials (`brain/team/provisioning.py::revoke_principal_access`).
- **No automatic return to solo.** Revoking the last member keeps
  `team_state() == "team"`, so a surviving key never becomes the implicit
  administrator. Only `brain team dissolve --yes` records `teams.dissolved_at`
  and returns to solo; it is refused while any member is live, and a member
  added later turns the team back on.
- **Admin credential minting** (`/admin/principals…`) refuses `*` (HTTP 422),
  refuses scopes the caller does not hold (403), and in team state refuses
  minting for another team member (403); member keys come from invitations or
  the operator CLI.
- **Admin disable/revoke** in team state refuses disabling one's own principal
  or revoking the credential in use (409), and refuses any change that would
  leave no live administrator credential (409, team row locked).
- **Dashboard password login** (`GET /api/web-login`, `POST /api/web-login/verify`)
  needs the team administrator in team state, like the write routes. The
  dashboard's password sign-in therefore works in team state only when its
  server key is an administrator credential; members sign in with their own key.
- **Telegram bridge**: in team state an `app: …` prefix or project slug only
  resolves to repositories the poller's own credential may read.

### Concurrency hardening (M1, [#157](https://github.com/Perlitten/project-brain-public/pull/157))

Found by the [multi-user memory load test](#multi-user-memory-load-test-2026-10-18).

- **One global row-lock order**, documented in the `brain/team/authorization.py`
  module docstring; every lock taker follows it and nothing upgrades a lock.
- **Deadlock-free revoke.** `revoke_member` takes its locks in that order and
  runs inside `retry_transient` (`brain/team/locking.py`): a savepoint retry on
  SQLSTATE 40P01/40001, at most 5 attempts with jitter. If every attempt fails,
  nothing is revoked and the team route answers 503 with `Retry-After`; a 2xx
  means every credential acting for the member is revoked.
- **Write fence** (`brain/team/write_fence.py`): before the first write of each
  transaction, the authorized identity (and a run's human initiator) is
  re-read FOR SHARE inside that transaction, so a write authorized before a
  revoke cannot land after it (403 instead).
- **Bounded team transactions**: team routes set `SET LOCAL lock_timeout`
  (`BRAIN_TEAM_LOCK_TIMEOUT_MS`, 5000) and `idle_in_transaction_session_timeout`
  (`BRAIN_TEAM_IDLE_TX_TIMEOUT_MS`, 30000); retryable lock failures
  (40P01/40001/55P03) answer 503 with `Retry-After`.
- **No `teams` convoy**: invitation creation takes a keyed
  `pg_advisory_xact_lock` on (team, invitee name) instead of `teams` FOR UPDATE;
  invitation accept takes its project locks up front in id order; the
  per-request `last_used_at` touch is throttled (60 s) and non-blocking
  (SKIP LOCKED).
- **`harness:write` in member scopes**, so a member's agent can record L1
  evidence; every harness write is still role-gated.

### Memory provenance and freshness (beyond M1)

These close the memory and freshness gaps found in the 2026-10-17 audit; they
serve solo and team state alike.

- **Webhook reindex follows origin** ([#128](https://github.com/Perlitten/project-brain-public/pull/128)): the post-merge job runs
  `git fetch origin` and checks out the delivered commit before indexing
  (`BRAIN_WEBHOOK_FETCH_SOURCE`, default `true`) instead of failing on a sha
  mismatch; `/repositories` reports `commits_behind_origin`.
- **Memory provenance** ([#129](https://github.com/Perlitten/project-brain-public/pull/129)): `author_principal_id` and
  `updated_by_principal_id` on decisions, rules, learnings, episodes and skills; editing a decision or rule
  keeps its id, bumps `version` and keeps the old content as a superseded
  history row; `expected_version` gives
  compare-and-set (HTTP 409 on a stale write); team-admin gate for
  `system_global`; in team state G2 approval needs two distinct principals and
  nobody approves their own proposal.
- **Freshness envelope** ([#130](https://github.com/Perlitten/project-brain-public/pull/130)): MCP tool replies and retrieval responses carry
  a `freshness` block: `state` (`fresh`, `stale_served` or `unknown`) with a
  `reason`, the indexed commit, its age and, where known, commits behind origin, with `BRAIN_FRESHNESS_MAX_AGE_S`
  (default 7 days) as the age threshold.
- **Origin poll and source mirror** ([#135](https://github.com/Perlitten/project-brain-public/pull/135)): an `origin_poll` job
  (`BRAIN_ORIGIN_POLL_INTERVAL_S`, default 300 s; `POST /jobs/origin-poll`)
  fetches each repository's origin into a Brain-owned bare mirror
  (`BRAIN_SOURCE_MIRROR_DIR`, remotes from `BRAIN_ORIGIN_REMOTES`), records it in
  `repository_origin_state` and reports `origin_ahead_of_index` in the freshness
  envelope. Freshness no longer depends on a webhook or a writable `/repo`, so
  `BRAIN_WEBHOOK_FETCH_SOURCE=false` is no longer needed. Each mirror is a full
  clone; summary enrichment still reads the checkout.

### Governed memory (M4)

- **Proposals and review queue** ([#137](https://github.com/Perlitten/project-brain-public/pull/137)): in team state a member's
  decision or rule becomes a `memory_proposals` row (HTTP 202); reviewers approve,
  reject or withdraw with `expected_version` (`GET /memory/proposals`, MCP
  `team_propose_*`, `team_list_proposals`, dashboard Memory → Review). Learnings
  have a personal tier that a member can propose for promotion
  ([MEMORY_REVIEW.md](../MEMORY_REVIEW.md)).
- **Learnings, skills and consolidation** ([#145](https://github.com/Perlitten/project-brain-public/pull/145)): a member's `POST /learnings` is
  personal by default and a shared learning is a proposal; member skills start
  `pending`; consolidation G2 counts distinct principals (team floor 2) and
  proposes instead of promoting; approvals record `approved_by_principal_id`;
  `memory.proposed|approved|rejected` activity and `pending_review_count` on
  `/team/me`.
- **Local surfaces and G4** ([#148](https://github.com/Perlitten/project-brain-public/pull/148)): `BRAIN_LOCAL_PRINCIPAL_KEY` makes stdio
  MCP and the CLI act as that principal (fails closed if the key is invalid);
  in team state G4 episodes go through proposals and evidence authors cannot
  approve them; optional Telegram review notices (`MEMORY_REVIEW_TELEGRAM_NOTIFY`).
- **Notices to every reviewer** ([#153](https://github.com/Perlitten/project-brain-public/pull/153)): in team state each pending
  proposal writes one in-app notice per member who may approve it (project
  reviewers, or team admins for unattached and system-global proposals; never
  the author or evidence suppliers): `GET /team/me/notifications`, mark read and
  read-all, a Notifications panel on `/team/me`. Members can link their own
  Telegram chat with a one-time code (`/team/me/telegram`); delivery needs
  `TEAM_MEMBER_TELEGRAM_NOTIFY` and the bot token. Solo state is unchanged.
- **Atomic CAS** ([#156](https://github.com/Perlitten/project-brain-public/pull/156)): decision and rule writes lock the canonical
  row and claim the version with a conditional `UPDATE`, so exactly one writer
  wins per `expected_version` and the rest get 409; blind writes form one linear
  chain; concurrent creates and rule CAS losers answer 409 instead of 500; of
  several proposals approved on one base version, one publishes and the others
  stay pending with 409.
- **Revoked members' personal learnings are retired** ([#156](https://github.com/Perlitten/project-brain-public/pull/156)): revoking a
  membership sets them to `personal_retired` in the same transaction. They stay
  for audit but are never read, proposed or promoted again, even if the
  principal rejoins ([MEMORY_REVIEW.md](../MEMORY_REVIEW.md#revoked-authors)).

### Bounded execution (M2)

Off by default; see [bounded team execution](../operations/TEAM_EXECUTION.md).

- **First slice** ([#136](https://github.com/Perlitten/project-brain-public/pull/136)): `BRAIN_EXECUTION_ENABLED` plus an execution root and
  a tool allowlist, and a per-member grant (`team_execution_grants`,
  `brain team execution grant|revoke|list|recover`); runs execute a fixed list
  of allowlisted tools in a fresh clone (`agent_run_executions`) under a lease
  with a fencing token, re-checking authority at every step; `run.execution.*`
  evidence.
- **Container backend, environments, egress** ([#143](https://github.com/Perlitten/project-brain-public/pull/143)): `BRAIN_EXECUTION_BACKEND=container`
  with CPU, memory, PID and tmpfs limits, named `BRAIN_EXECUTION_ENVIRONMENTS`,
  and an egress proxy that allows only listed `host:port` pairs.
- **Hardening** ([#147](https://github.com/Perlitten/project-brain-public/pull/147)): dependencies are rechecked when execution is
  requested and at the first execution checkpoint; doctor reports
  `execution_config` (warns for the subprocess backend).
- **Consent, budget, artifacts, real-docker CI** ([#154](https://github.com/Perlitten/project-brain-public/pull/154)): an execution may
  declare `llm: {provider, model, max_tokens, max_cost_cents?}` against
  providers in `BRAIN_EXECUTION_LLM_PROVIDERS`; egress to an LLM host is refused
  unless the execution declares the provider that owns it. In team state each
  use needs a per-project consent (`team_llm_consents`) and a per-project LLM
  budget (`team_llm_budgets`, tokens and/or cents per month or in total, with
  optional per-member limits); without a budget LLM use fails closed. Declared
  ceilings are reserved in full, because Brain cannot meter inside the step's
  TLS connection. Checks run at request time and at every checkpoint (API
  `…/execution/llm-consents` and `…/execution/llm-budget`, CLI
  `brain team execution consent|budget`). Step logs, reports and a
  `final.patch` are stored per run in `agent_run_execution_artifacts`, redacted
  and capped (`BRAIN_EXECUTION_ARTIFACT_MAX_BYTES`,
  `BRAIN_EXECUTION_ARTIFACTS_MAX_BYTES`). A `container-real-docker` CI job runs
  the container backend against a real Docker daemon with test images.
  Agent-chosen steps are deferred ([rationale](../operations/TEAM_EXECUTION.md#why-agent-chosen-steps-are-deferred)).

### Coordination and board (M3, M5)

- **First slice** ([#142](https://github.com/Perlitten/project-brain-public/pull/142)): subtasks with a dependency DAG, declared-scope
  overlap warnings, instruction-resolution versions per run, fenced resource
  leases and an integration queue (`team_task_plans`, `team_resource_leases`,
  `team_run_resolutions`, `team_integration_entries`; `brain team coord`;
  read-only MCP tools). See [team coordination](../TEAM.md#team-coordination-m3).
- **Board, conflict view, MCP writes** ([#146](https://github.com/Perlitten/project-brain-public/pull/146)): `/team/board` with task columns,
  leases, overlaps and the queue; a conflict view with actions; MCP
  `team_claim_lease`, `team_renew_lease`, `team_release_lease`,
  `team_declare_scope`, `team_enqueue_integration`. Creating subtasks and
  integrate/reject stay HTTP-only.
- **Hardening** ([#147](https://github.com/Perlitten/project-brain-public/pull/147)): integrating an entry completes its task; overlap
  also compares the paths a run actually changed; a service-level
  `force_release_lease` records `coordination.lease_force_released`;
  PostgreSQL race tests (not run in CI).
- **M3 leftovers** ([#152](https://github.com/Perlitten/project-brain-public/pull/152)):
  - admin force-release of a lease with a required reason and an optional
    fencing-token CAS: `POST /team/projects/{project_id}/leases/{lease_id}/force-release`,
    MCP `team_force_release_lease`, CLI `brain team coord force-release`; team
    admins only, the old holder's token is bumped, and the web activity view
    labels every activity kind (a parity test fails on a missing label);
  - graph-dependency overlap: open tasks whose scopes share no path but where
    one file imports the other (`IMPORTS` edges in the code graph) get a
    `kind: "dependency"` warning; bounded, and degrades to path-only overlap
    with a `dependency_graph` status (`ok`, `stale`, `unavailable`, `skipped`);
  - a real combined patch at integration (`brain/team/combined_patch.py`): the
    integrated patches and the run's patch are applied to the base in a
    throwaway bare repository; clean → integrated, conflict → `rejected`,
    check cannot run → `needs_rebase`. Nothing is merged or pushed.
- **Doctor and cloud packaging** ([#140](https://github.com/Perlitten/project-brain-public/pull/140)): `brain team doctor [--json]` and
  `GET /team/doctor`; `deploy/team/` compose overlay with Caddy, web and an
  executor worker profile ([TEAM_DEPLOY.md](../operations/TEAM_DEPLOY.md)).
- **HTTPS front checked end to end** ([#151](https://github.com/Perlitten/project-brain-public/pull/151)): `scripts/team_proxy_check.py`
  and the "Team proxy" workflow start the real prod compose and `deploy/team/`
  overlay with the real Caddyfile (local certificates) and assert redirects,
  SNI and Host handling, health exposure, client addresses, OIDC cookies,
  security headers and body limits
  ([TEAM_DEPLOY.md](../operations/TEAM_DEPLOY.md#checking-the-https-front)).
  It fixed four defects in `deploy/team/`:
  - a shared throttle bucket: caddy and web now have fixed addresses on an
    `edge` network (`172.31.250.0/24`) and the overlay sets
    `BRAIN_TRUSTED_PROXY_IPS` to exactly those two;
  - no limit on chunked bodies: `request_body max_size 25MiB`;
  - public `/health` and `/ready`: they now answer 404 without the server key
    (`forward_auth` to the admin-only diagnostics route);
  - no dashboard security headers: Caddy adds HSTS, nosniff, X-Frame-Options,
    Referrer-Policy and Permissions-Policy without overriding the app's own.

Tests: the `tests/test_team_*.py` suite (identity, invitations, OIDC, OAuth
link, transport, execution, coordination, registry, notifications, lock order
and more), plus `tests/test_memory_provenance.py`, `tests/test_memory_proposals.py`,
`tests/test_memory_cas_concurrency.py`, `tests/test_webhook_fetch.py` and the
PostgreSQL-only restore and concurrency cases that CI runs. The container
backend runs against a real Docker daemon in CI (#154); the coordination race
tests do not run in CI. The [multi-user memory load test](#multi-user-memory-load-test-2026-10-18)
is a manual run, not a CI job.

## Multi-user memory load test (2026-10-18)

`scripts/team_memory_load_e2e.py` (#155, [runbook](../runbooks/team-memory-load-e2e.md),
[report](../reports/team-memory-load-e2e-2026-10-18.md)) runs a real API and
worker against throwaway PostgreSQL and Redis with mock LLM and embedding
providers, N principals × 50 mixed operations each, plus CAS, revocation,
onboarding, lease and governance scenarios. Runs were on Windows 11 with
Docker in WSL. Harness fixes for Windows multi-worker stalls and clock skew
are in #159.

| | First run (main 411dfc2, before #156/#157) | Final `main` (ca4b727) |
|---|---|---|
| 12×50, 1 API worker | 40 pass / 9 fail | **PASS 50/0**, no invariant violations |
| 36×50, 2 API workers | 36 pass / 13 fail | 0 deadlocks, 0 5xx; failed only on two harness artefacts, both fixed in #159 |
| Deadlocks in the API log | 79 / 75 | 0 / 0 |
| 5xx | 34 / 32 | 0 / 0 |
| Decision CAS | lost updates: 2–3 winners per round | 5 rounds, exactly one winner each, 0 lost writes |
| Rule CAS, create race, concurrent approvals, approve vs reject | 500s and several winners | one winner, the rest 409 |
| Revoke | deadlocked (500) | 12×50: 200 on the first attempt in 0.77 s; 36×50: 200 in 0.16 s |
| After the revoke | writes in flight landed after `revoked_at` | 12×50: 67 requests, all 401, 0 late writes, 3 proposals withdrawn, personal learnings retired; 36×50: 270 requests, all 401 |
| Concurrent invitation accept | 23 × 500 (36×50) | no 5xx |

Final `main` at 12×50: 601 load requests in 58.9 s (10.2 rps); status mix over
the whole run 200:676, 202:113, 401:68, 403:94, 409:78, 422:1, 429:2. The first
run's 12.2 s p95 on `POST /team/invitations` (36×50) came from the `teams` row
convoy that #157 removed; no final p95 for that route is quoted here.

The final 36×50 run failed only on harness artefacts:

- **8 requests stalled for 120 s.** On Windows, `uvicorn --workers` shares one
  listening socket; a worker that loses the `accept()` race freezes its event
  loop. Linux and production are not affected. #159 starts one API process per
  port on Windows.
- **6 rows flagged as written after the revoke.** Clock skew: Docker Postgres
  in WSL ran about 467 ms ahead of the Windows host. All of those rows were
  already `withdrawn` or `personal_retired`, states only the revoke sets, so
  they committed before it. #159 compares on the database clock.

With #159, 36×50 runs passed: 3 of 4 earlier runs (the fourth failed only on
the clock-skew check, before that fix) plus a verification run; see #159 for
the run table.

Remaining observation: at 12×50, latency is about 1.5× the first run's. The
likely cause is the write fence's three extra indexed reads per write
transaction plus load on the test machine; it has not been profiled.

## Known gaps (checked against `main` ca4b727 on 2026-10-18)

Closed since the 2026-10-17 snapshot: SSO (#139, #144), invitation UI (#132),
per-process throttle (#133), `brain/workspace` and `brain/team` as two models
(#141, #149), the `/late-interaction/status` 500 (#133), freshness only via
webhook (#135), no `/team/me` or activity feed (#134). Closed in wave 4: the
untested Caddy/TLS front (#151); M2 provider consent, LLM budget, artifacts
and a real-docker CI job (#154); M3 graph-dependency overlap, combined patch,
an operator surface for force-release and the missing
`coordination.lease_force_released` label (#152); offline CLIs on the registry
file (#153); review notices only to the owner (#153); a revoked member's
personal learnings kept readable (#156); decision CAS lost updates and 500s on
CAS and create races (#156); revocation and onboarding deadlocks, writes in
flight after a revoke, the `teams` row convoy and members without
`harness:write` (#157). Still open:

- **Not deployed.** Production still runs c13d056, a release from before team
  mode.
- **Not exercised end to end with people**: no pilot yet (M6 needs humans);
  production upgrade/restore, external identities and secrets are untested. The
  scripted e2e check (#138), the proxy check (#151) and the memory load test
  (#155) drive throwaway Docker stacks, not real users.
- **Admin read scope needs an owner decision.** [TEAM.md](../TEAM.md) says a
  team administrator without a project role reads every project's memory
  (decision and rule lists, and the Brain-wide episode, learning and skill
  lists), but the [ADR permission matrix](../adr/team-collaboration.md#permission-matrix)
  says "Only with project membership". Behaviour and ADR were deliberately left
  unchanged in #157; one of them has to move.
- **M2 execution:**
  - steps are a fixed operator list; agent-chosen steps are deferred
    ([rationale](../operations/TEAM_EXECUTION.md#why-agent-chosen-steps-are-deferred));
  - token accounting reserves the declared ceiling in full, because Brain
    cannot meter inside a step's TLS connection; a tool that ignores its
    declared `max_tokens` can spend more at the provider.

  Also not built: external-effect idempotency, an executor image recipe
  (images must contain git), resuming fenced runs, artifact retention, a
  dashboard or MCP surface for requesting and inspecting executions.
- **M3 coordination:** coordination does not merge, push, build or test the
  combined patch; the PostgreSQL coordination race tests do not run in CI.
- **Memory integrity edges:**
  - decision history has no unique index on (canonical id, version); the chain
    is protected only by the row lock and the conditional `UPDATE` (#156);
  - `sync_rules_from_yaml` (`brain rules sync`) reads rules without the row
    lock, so it is not serialized against concurrent rule writes;
  - duplicate decision versions written before #156 are not detected or
    repaired, and members revoked before #156 keep `personal` learnings until
    someone retires them. Production (c13d056) has neither decision versions
    nor team members, so this only affects a database that ran an earlier
    `main` (see [Deploy implications](#deploy-implications));
  - without `BRAIN_LOCAL_PRINCIPAL_KEY` the local MCP and CLI act as the
    operator.
- **Workspace registry:** until the API has imported its file, a team-state
  worker falls back to its own file; a team dissolved back to solo reads the
  file again, which may be older than the table;
  `GET /workspace/repositories/{id}/events` still denies a disabled entry even
  to an administrator; the offline CLIs probe the database on every run
  (bounded to 10 s).
- **Throttle and SSO edges:** if the dashboard's egress IP is not fixed (for
  example on Vercel), invitees share one throttle bucket; a browser closed
  without logout keeps its SSO session credential until the next sign-in or
  unbind.
- **Native MCP:** streaming through Caddy (`flush_interval -1`) has not been
  tested against a live host (the proxy check does not exercise MCP), and
  native MCP still needs a single API worker.
- **No email or webhook notifications.** Invitations and review notices are
  not emailed; the administrator passes the invitation link, and notices go to
  the in-app inbox and opt-in Telegram only.
- **Windows development:** do not run the API with `uvicorn --workers` on
  Windows; a worker that loses the `accept()` race freezes (#159). Linux and
  production are not affected.

## Deploy implications

Everything below applies to the first deployment of `main` after c13d056. The
solo step is in [solo-deploy-c13d056-to-main.md](../release/solo-deploy-c13d056-to-main.md);
the team step in [TEAM_DEPLOY.md](../operations/TEAM_DEPLOY.md).

**New settings** (all optional; every feature is off by default or team-only):

| Setting | Default | PR |
|---|---|---|
| `BRAIN_ORIGIN_POLL_INTERVAL_S`, `BRAIN_ORIGIN_POLL_TIMEOUT_S` | 300, 30 | #135 |
| `BRAIN_SOURCE_MIRROR_DIR`, `BRAIN_ORIGIN_REMOTES` | empty (prod compose mounts `source_mirrors` at `/var/lib/brain/mirrors`) | #135 |
| `BRAIN_EXECUTION_ENABLED`, `BRAIN_EXECUTION_ROOT`, `BRAIN_EXECUTION_TOOLS` and the other `BRAIN_EXECUTION_*` limits | off | #136 |
| `BRAIN_EXECUTION_BACKEND`, `BRAIN_EXECUTION_ENVIRONMENTS`, `BRAIN_EXECUTION_DOCKER`, `BRAIN_EXECUTION_CONTAINER_*`, `BRAIN_EXECUTION_EGRESS_PROXY_IMAGE`, `BRAIN_EXECUTION_EGRESS_UPSTREAM_NETWORK`; `BRAIN_EXECUTION_EGRESS` now takes `host:port` | `subprocess`, `{}`, `docker`, … | #143 |
| `BRAIN_OIDC_ENABLED`, `_ISSUER`, `_CLIENT_ID`, `_CLIENT_SECRET`, `_REDIRECT_URI`, `_SCOPES`, `_ALLOWED_EMAIL_DOMAINS`, `_REQUIRE_FOR_INVITATIONS` | off | #139 |
| `BRAIN_BACKUP_DIR`, `BRAIN_BACKUP_MAX_AGE_HOURS` (doctor) | empty, 48 | #140 |
| `BRAIN_WEB_DOMAIN`, `BRAIN_API_DOMAIN`, `BRAIN_ACME_EMAIL`, `BRAIN_BACKUP_HOST_DIR`, `BRAIN_EXECUTION_HOST_DIR`, `BRAIN_PLAIN_WORKER_REPLICAS` (only in the `deploy/team/` overlay) | — | #140 |
| `BRAIN_TRUSTED_PROXY_IPS` | empty | #144 |
| `BRAIN_LOCAL_PRINCIPAL_KEY` | empty (local surfaces act as the operator) | #148 |
| `MEMORY_REVIEW_TELEGRAM_NOTIFY` (needs `TELEGRAM_ALERTS_ENABLED`, token and chat) | `false` | #148 |
| `BRAIN_EDGE_SUBNET`, `BRAIN_CADDY_IP`, `BRAIN_WEB_IP` (only in the `deploy/team/` overlay: the `edge` network with fixed caddy and web addresses; the overlay sets `BRAIN_TRUSTED_PROXY_IPS` to the two addresses) | `172.31.250.0/24`, `172.31.250.10`, `172.31.250.11` | #151 |
| `TEAM_MEMBER_TELEGRAM_NOTIFY` (needs `TELEGRAM_ALERT_BOT_TOKEN`) | `false` | #153 |
| `BRAIN_EXECUTION_LLM_PROVIDERS` (JSON map of provider name to hosts and optional models) | `{}` | #154 |
| `BRAIN_EXECUTION_ARTIFACT_MAX_BYTES`, `BRAIN_EXECUTION_ARTIFACTS_MAX_BYTES` (per artifact, per run) | 1048576 (1 MiB), 8388608 (8 MiB) | #154 |
| `BRAIN_TEAM_LOCK_TIMEOUT_MS`, `BRAIN_TEAM_IDLE_TX_TIMEOUT_MS` (team routes; 0 disables a bound) | 5000, 30000 | #157 |

`BRAIN_WEBHOOK_FETCH_SOURCE=false` is no longer needed once the origin poll
runs (#135).

Not every setting is in `.env.example`: by git history only #136, #143, #144,
#148, #153, #154 and #157 touched it (the #151 edge addresses are in
`deploy/team/.env.team.example`), so the origin-poll, OIDC and backup settings are
documented in `brain/config/settings.py`, [DEPLOYMENT.md](../operations/DEPLOYMENT.md),
[TEAM_SSO.md](../operations/TEAM_SSO.md) and [TEAM_DEPLOY.md](../operations/TEAM_DEPLOY.md)
instead.

**Schema.** All changes are additive and created on first boot:

- New tables: `team_activity_events` (#134), `repository_origin_state` (#135),
  `team_execution_grants`, `agent_run_executions` (#136), `memory_proposals`
  (#137), `team_identities`, `team_oidc_flows` (#139), `team_task_plans`,
  `team_resource_leases`, `team_run_resolutions`, `team_integration_entries`
  (#142), `workspace_registry_entries`, `workspace_registry_events` (#149),
  `team_notifications`, `team_telegram_links` (#153), `team_llm_consents`,
  `team_llm_budgets`, `agent_run_execution_artifacts` (#154).
- New columns: `team_invitations.verified_issuer` and `verified_subject` (#139),
  `agent_run_executions.environment` (#143),
  `team_identities.session_credential_id` (#144), `approved_by_principal_id` on
  decisions, rules, `memory_learnings` and `memory_skills` (#145),
  `agent_run_executions.llm` (#154).
- New value: the `memory_learnings.status` value `personal_retired` (#156), set
  when a membership is revoked. No column or constraint change.

**Learnings of members revoked before #156.** Revocation retires personal
learnings only from #156 on; there is no script or CLI for members revoked
earlier. Production (c13d056) has no team tables, members or personal tier, so
there is nothing to retire there. On any other database that ran an earlier
`main` with revoked members, retire them by hand once: set `status` to
`personal_retired` on `memory_learnings` rows with `status = 'personal'` whose
`author_principal_id` belongs to a `team_members` row with `revoked_at` set,
after a backup, and only if that principal has not been re-added.

**External `/health` and `/ready` behind Caddy (#151).** Through the
`deploy/team/` front they now answer 404 without the server key (`forward_auth`
to the admin-only diagnostics route), so external uptime probes must send the
key or move to another check. Host-local `127.0.0.1:8010/health`
(`server_up.sh`, compose healthchecks) is unaffected.

**Workspace registry: file to table (#149).** On its first team-state start the
API imports its own `/app/.brain/workspace/repository_registry.json` into
PostgreSQL once (worker files are never imported); solo state keeps using the
file. In the production compose that file lives in the `brain-api` container's
writable layer and is lost when the container is recreated, so copy it out
**before** `server_up.sh` and back afterwards, restart the API, and check its log
for "workspace registry". The exact commands are in
[TEAM_DEPLOY.md](../operations/TEAM_DEPLOY.md#workspace-registry-file-to-table).

**Ship web and API together.** The SSO logout (#144), the board (#146), the
invitation pages (#132), the notifications and Telegram panels on `/team/me`
(#153) and the activity labels (#152, #154) need both halves of the same
release.

**Before a team deploy**, run the [team e2e check](../runbooks/team-e2e-check.md)
and the [proxy check](../operations/TEAM_DEPLOY.md#checking-the-https-front)
against the candidate and `brain team doctor` on the host.

## Next steps, in order

1. Deploy `main` to production in solo state (registry copied out and back),
   only after the owner's explicit go-ahead, and confirm nothing changed for
   the owner.
2. Run the one-time retire for members revoked before #156 on any database
   that has them ([above](#deploy-implications)); production has no team
   members yet, so there it is a check that finds nothing.
3. Add one real member on one project and walk the invite → sign-in (key and
   SSO) → revoke path on the deployed host.
4. Get the owner's decision on admin read scope (TEAM.md versus the ADR
   permission matrix) and align the code or the ADR.
5. Run the M6 pilot with 3–5 people on two projects.
