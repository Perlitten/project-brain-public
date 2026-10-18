# Team mode: current status

Snapshot: 2026-10-18, `main` after PRs #132–#149 (invitation UI, shared
throttle, `/team/me` and activity feed, origin poll, bounded execution,
governed memory, team e2e check, OIDC SSO, doctor and cloud packaging, one
repository authority, coordination, board, hardening, local principal, shared
workspace registry). Update this file in
the same PR as any team-mode change; it is the one place that answers "what
stage is team mode at". The contract is the [ADR](../adr/team-collaboration.md);
the per-requirement evidence is the [foundations matrix](team-foundations.md).

## In one paragraph

**M1** (identity, membership, fail-closed authorization, invitations with a
dashboard UI, optional OIDC SSO, one repository authority) is implemented on
`main`: a solo Brain is a team of one, and adding the first verified member
switches repository-scoped API, MCP and dashboard requests to per-project
authorization. First slices of **M2** (bounded execution, off by default),
**M3** (coordination, board, conflict view) and **M5** (doctor, cloud
packaging) are on `main`, and the **M4** write paths (decisions, rules,
learnings, skills, consolidation) are governed by proposals and review. Each of
these has named gaps below. Team mode is **not deployed** (production runs
c13d056) and has **not been tried with real people**; green CI and the
scripted [team e2e check](../runbooks/team-e2e-check.md) are the only evidence
so far, and the ADR says plainly that green CI is not team readiness.

## Stages

| Stage | Scope | Status |
|---|---|---|
| M0 | Contract, permission/revocation semantics, coverage matrix | Done |
| M1 | Verified identity, membership, centralized authorization, provisioning, invitations, restore fence | Implemented, including the invitation UI (#132), OIDC SSO (#139, #144) and one repository authority (#141, #149); not deployed |
| M2 | Bounded real executors, tool/secret/egress/budget capabilities, revocation checkpoints | First slices, off by default: grants, leases with fencing, subprocess and container backends, execution environments, egress proxy (#136, #143, #147). Not yet: provider consent and LLM budget, agent-chosen steps, artifacts, a real-docker CI job |
| M3 | Subtasks, coordinator, overlap detection, integration queue | First slice plus hardening: subtask DAG, declared-scope and changed-files overlap warnings, instruction versions, fenced leases, integration queue, MCP coordination writes (#142, #146, #147). Not yet: graph-dependency overlap, a real merge or combined patch, an operator surface for force-release |
| M4 | Governed versioned memory, independent review | Write paths governed: provenance and CAS (#129); member decisions and rules become proposals with a review queue and a personal learnings tier (#137); member learnings and skills, consolidation and G4 episodes go through proposals; nobody approves their own (#145, #148). Not yet: review notices beyond the owner's Telegram chat |
| M5 | Invitation/board/conflict UX, cloud packaging, upgrade/restore diagnostics | Invitation UI (#132), `/team/me` and activity feed (#134), doctor and cloud packaging (#140), board and conflict view (#146). The Caddy/TLS front has not run on a live host |
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
- **Doctor and cloud packaging** ([#140](https://github.com/Perlitten/project-brain-public/pull/140)): `brain team doctor [--json]` and
  `GET /team/doctor`; `deploy/team/` compose overlay with Caddy, web and an
  executor worker profile ([TEAM_DEPLOY.md](../operations/TEAM_DEPLOY.md)).

Tests: the `tests/test_team_*.py` suite (identity, invitations, OIDC, OAuth
link, transport, execution, coordination, registry and more), plus
`tests/test_memory_provenance.py`, `tests/test_memory_proposals.py`,
`tests/test_webhook_fetch.py` and the PostgreSQL-only restore and concurrency
cases that CI runs. The coordination race tests and the container backend
against a real Docker daemon are not run in CI.

## Known gaps (checked against `main` on 2026-10-18)

Closed since the 2026-10-17 snapshot: SSO (#139, #144), invitation UI (#132),
per-process throttle (#133), `brain/workspace` and `brain/team` as two models
(#141, #149), the `/late-interaction/status` 500 (#133), freshness only via
webhook (#135), no `/team/me` or activity feed (#134). Still open:

- **Not deployed.** Production still runs c13d056, a release from before team
  mode.
- **Not exercised end to end with people**: no pilot yet (M6 needs humans);
  production upgrade/restore, external identities and secrets are untested. The
  scripted e2e check (#138) drives a throwaway Docker stack, not real users.
- **Ops: the Caddy/TLS front is untested.** The `deploy/team/` Caddyfile has not
  been validated or run on a live host.
- **M2 execution:**
  - no provider consent or LLM budget;
  - steps are a fixed operator list, not chosen by the agent;
  - no artifacts;
  - no CI job runs the container backend against a real Docker daemon.

  Also not built: external-effect idempotency, an executor image recipe
  (images must contain git), resuming fenced runs.
- **M3 coordination:**
  - overlap does not look at graph dependencies, only declared scopes and
    changed files;
  - there is no real merge or combined-patch validation;
  - administrator force-release of a lease exists only in the service layer
    (`coordination.force_release_lease`), with no API route, MCP tool or CLI;
  - the web activity view does not render `coordination.lease_force_released`.

  PostgreSQL race tests do not run in CI, and doctor does not report orphaned
  runs.
- **Workspace registry:** the offline CLIs (`brain.lab.cli`, `brain.freshness.cli`,
  `brain.experiments.cli`, `brain.portfolio.cli`) and `brain/control/plane.py`
  still read the registry file, not the PostgreSQL table. Until the API has
  imported its file, a team-state worker falls back to its own file; a team
  dissolved back to solo reads the file again, which may be older than the
  table; `GET /workspace/repositories/{id}/events` still denies a disabled entry
  even to an administrator; the workspace CLI probes the database on every run.
- **Memory:** review notifications go only to the owner's Telegram chat; a
  revoked member's personal learnings stay stored; without
  `BRAIN_LOCAL_PRINCIPAL_KEY` the local MCP and CLI act as the operator.
- **Throttle and SSO edges:** if the dashboard's egress IP is not fixed (for
  example on Vercel), invitees share one throttle bucket; a browser closed
  without logout keeps its SSO session credential until the next sign-in or
  unbind. Native MCP
  still needs a single API worker.
- **Invitations are not emailed**; the administrator passes the link.

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

`BRAIN_WEBHOOK_FETCH_SOURCE=false` is no longer needed once the origin poll
runs (#135).

Not every setting is in `.env.example`: by git history only #136, #143, #144
and #148 touched it, so the origin-poll, OIDC and backup settings are
documented in `brain/config/settings.py`, [DEPLOYMENT.md](../operations/DEPLOYMENT.md),
[TEAM_SSO.md](../operations/TEAM_SSO.md) and [TEAM_DEPLOY.md](../operations/TEAM_DEPLOY.md)
instead.

**Schema.** All changes are additive and created on first boot:

- New tables: `team_activity_events` (#134), `repository_origin_state` (#135),
  `team_execution_grants`, `agent_run_executions` (#136), `memory_proposals`
  (#137), `team_identities`, `team_oidc_flows` (#139), `team_task_plans`,
  `team_resource_leases`, `team_run_resolutions`, `team_integration_entries`
  (#142), `workspace_registry_entries`, `workspace_registry_events` (#149).
- New columns: `team_invitations.verified_issuer` and `verified_subject` (#139),
  `agent_run_executions.environment` (#143),
  `team_identities.session_credential_id` (#144), `approved_by_principal_id` on
  decisions, rules, `memory_learnings` and `memory_skills` (#145).

**Workspace registry: file to table (#149).** On its first team-state start the
API imports its own `/app/.brain/workspace/repository_registry.json` into
PostgreSQL once (worker files are never imported); solo state keeps using the
file. In the production compose that file lives in the `brain-api` container's
writable layer and is lost when the container is recreated, so copy it out
**before** `server_up.sh` and back afterwards, restart the API, and check its log
for "workspace registry". The exact commands are in
[TEAM_DEPLOY.md](../operations/TEAM_DEPLOY.md#workspace-registry-file-to-table).

**Ship web and API together.** The SSO logout (#144), the board (#146) and the
invitation pages (#132) need both halves of the same release.

**Before a team deploy**, run the [team e2e check](../runbooks/team-e2e-check.md)
against the candidate and `brain team doctor` on the host.

## Next steps, in order

1. Deploy `main` to production in solo state (registry copied out and back)
   and confirm nothing changed for the owner.
2. Add one real member on one project and walk the invite → sign-in (key and
   SSO) → revoke path on the deployed host.
3. Close the M3 and M2 gaps the pilot needs: an operator surface for lease
   force-release, a real-docker CI job, consent and budget for execution.
4. Move the offline CLIs to the PostgreSQL registry.
5. Run the M6 pilot only after those stages can exercise coordinated runs,
   overlaps, revocations and crashes with real people.
