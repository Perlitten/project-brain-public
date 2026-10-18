# Team mode walkthrough

> **Experimental preview, `main` only, not in v0.9.2.** Team mode is not
> deployed anywhere and has not been piloted with real people. Read the
> [known limitations](#known-limitations) before you invite anyone. Current
> stage and gaps: [team-mode status](design/team-status.md).

This is the one end-to-end path for an operator: create a team, attach a
repository, become its first administrator, invite a member, and let that
member use the dashboard and connect an agent over MCP. It ends with revoking
them. Contract and design: [ADR: team collaboration](adr/team-collaboration.md).

## Before you start

- A Brain built from `main`, running with `BRAIN_STORAGE_MODE=full` (the
  default). Lite has no team layer; every `brain team` command refuses it.
- Shell access to the server (or the API container) with the server's `.env`
  loaded. The first administrator can only be created there.
- HTTPS in front of the API if anyone will redeem an invitation or use a key
  from another machine ([Deployment](operations/DEPLOYMENT.md)). For one team
  on one host, [one-team cloud deployment](operations/TEAM_DEPLOY.md) packages
  the API and dashboard behind Caddy with automatic TLS.
- `brain team doctor` checks the team setup read-only (state, administrators,
  SSO settings, revocations, restore fence, stuck executions, source freshness,
  backups) and prints a fix for each problem.
- The repository you want to share, indexed on the server. Note the path it
  was indexed under: that exact string is the repository's identity.

A Brain with no team members is a **team of one**: every authenticated caller,
the shared `PROJECT_BRAIN_API_KEY` included, can use every repository, and
`/health` reports `"mode": "solo"`. Accepting the first invitation switches it
to `"mode": "team"`. From then on the shared key and keyless development access
are refused with `Named credential required`, and each person uses their own
`pbk_` key.

## 1. Create the team and a project

On the server:

```bash
brain team init --name "My team"        # once; idempotent
brain team project add core             # once per project
brain team project list                 # prints project ids (UUIDs)
```

A project groups repositories and carries the policy its members' roles are
narrowed by. `--permission` (repeatable) limits that policy; the default allows
everything a role can grant.

## 2. Attach the repository

```bash
brain team repo attach --project core --repository /repo
brain team repo list --project core
```

`--repository` takes the repository id or the **path exactly as indexed**; a
path Brain has not indexed fails with `index it first`. A repository belongs to
at most one project. In team state, a repository that is not attached to any
project is reachable only by team administrators.

## 3. Become the first administrator

Still on the server, issue an administrator invitation to yourself and redeem
it:

```bash
brain team invite create --admin --name owner --project core
# The pbi_ token is printed once, to stderr.
brain team invite accept                 # prompts for the token (hidden)
# or, from your workstation, against the HTTPS API:
brain team invite accept --api-url https://brain.example.com
```

`accept` prints a `pbk_` member key once. Store it in your password manager
before anything else: from this moment the Brain is in team state and the
shared key no longer works for project data.

Instead of `brain team invite accept` you can paste the token on the
dashboard's `/invite` page (see step 4). It works while the dashboard is still
in solo sign-in, shows the `pbk_` key once and signs you in with it.

Guard rails: the API refuses to create invitations while the Brain is solo
(HTTP 409), so the first one is always created on the server; and the first
invitation must be `--admin`. Details: [Inviting team members](operations/TEAM_INVITATIONS.md).

## 4. Invite a member

**From the dashboard** (signed in with your own administrator key): open
**Team → Invite a member**, fill in a name (optional; Brain picks
`invitee-…` when it is empty), an optional email, the projects and the role,
whether they are a team admin, and how long the invitation is valid (1, 3, 7
or 30 days). **Create invitation** shows an invitation link of the form
`https://<dashboard>/invite#token=pbi_…` and the bare token, **once**. Copy the
link and send it privately. The token sits after the `#`, so browsers don't
send it to any server and it never lands in an access log.

The **Invitations** panel on the same page lists every invitation with its
state (`invited`, `verified`, `active`, `expired`, `revoked`), its access and
its dates. **Revoke** stops an unused link. For an accepted invitation the
button reads **Revoke member**: it revokes the member it created as well (see
step 7). You cannot revoke your own membership there. If Brain does not
answer, the panel says so rather than showing an empty list.

Or on the server:

```bash
brain team invite create --name alice --project core --role contributor
```

or over the API with your own `pbk_` key:

```bash
curl -sS -X POST "$BRAIN_API_URL/team/invitations" -H "X-API-Key: $ADMIN_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"name": "alice", "projects": ["core"], "role": "contributor", "expires_hours": 48}'
# -> {"invitation": {...}, "token": "pbi_..."}
```

Roles are `reader` (read), `contributor` (read, create tasks, request runs,
propose knowledge) and `reviewer` (contributor plus approve and accept). A
role only narrows the project's policy. The default expiry is 72 hours, at
most 720.

The `pbi_` token is a **bearer secret**: whoever presents it first becomes
Alice. Send it over a private channel. The `email` field, if you set one, is
recorded for you and never checked.

**Alice opens the link** on the dashboard. The `/invite` page needs no
sign-in. It moves the token into the form and clears it from the address bar;
**Accept invitation** makes her a member, shows her `pbk_` key **once** with a
copy button, and signs her in with that key exactly as if she had pasted it on
the sign-in page (step 5). If the link is wrong, expired or revoked, the page
says so in one message, because Brain gives one answer for all three on
purpose. A link that was already accepted never shows the key again.
"Too many attempts" means the throttle refused the call (see Known
limitations). "Brain isn't answering" means the token was not used and she can
try again. The page is off when the dashboard runs with `BRAIN_WEB_ACTIONS=0`
or on demo data.

Or she redeems the token from a machine with the `brain` CLI installed:

```bash
brain team invite accept --api-url https://brain.example.com
```

The CLI prompts for the token and prints her `pbk_` key once. Plain `http` is
accepted only to loopback. Without the CLI she can `POST /team/invitations/accept`
with `{"token": "pbi_..."}`; that call needs no credential.

Alternatives to invitations, both administrator-only: the dashboard **Team**
page adds a member directly and shows their key once, and
`brain team member add --principal alice --mint-key --project core --role contributor`
does the same on the server. Keys from all three paths carry the same scopes.
A member key minted before PR #124 kept only `team:*` scopes and cannot open
the dashboard or search; issue a new one.

## 5. The member signs in to the dashboard

Alice opens the dashboard URL. Because the Brain has members, the sign-in page
asks for a **Member key**; she pastes her `pbk_` key. The dashboard checks it
with `GET /api/web-session`, which accepts only a live human team member, and
keeps it in a sealed, `httpOnly` cookie for 7 days. Every page and action is
then sent with her key, so Brain applies her roles: she sees the projects she
has a role in, and administrator pages show "Team admins only". Details:
[team sign-in](../apps/web/README.md#team-sign-in).

## 6. The member connects an agent over MCP

The same `pbk_` key authenticates her agent. Use one of the two remote
transports; local stdio connects straight to the databases and is for the
operator only.

**A. HTTP-backed stdio bridge** (works without native MCP being exposed):

```bash
export BRAIN_API_URL="https://brain.example.com"
export BRAIN_API_KEY="pbk_..."          # her member key
export BRAIN_REPO="/repo"               # the path the server indexed
```

The bridge is `python -m apps.mcp_server.remote_server`, run from a checkout of
this repository with its Python dependencies installed; it needs no local
datastores. Use [`examples/claude-code-stdio.mcp.json`](../examples/claude-code-stdio.mcp.json)
as the client configuration: it reads the three variables from the environment,
so the key never lands in a tracked file.

**B. Native Streamable HTTP** at `/mcp`, if the operator exposed it
([checklist](operations/DEPLOYMENT.md#native-mcp-and-oauth)):

```bash
claude mcp add --transport http project-brain https://brain.example.com/mcp \
  --header "Authorization: Bearer pbk_..."
```

**Which `repo_path` to pass.** Tools take a `repo_path` (the bridge defaults it
to `BRAIN_REPO`). It must be the path the **server** indexed, for example
`/repo`, not the member's local checkout path. Brain matches it against the
indexed repositories and then checks the member's role in the project that
repository is attached to. A path it cannot match, or a repository attached to
no project, is refused for anyone but an administrator. To see the paths she
can use, she can call `GET /repositories` with her key; it lists only
repositories she can read.

Both transports serve 33 tools: the personal ones plus `team_me`,
`team_activity`, `team_list_tasks`, `team_create_task` and `team_request_run`,
the five memory review tools (`team_propose_decision`, `team_propose_rule`,
`team_list_proposals`, `team_approve_proposal`, `team_reject_proposal`), and
nine coordination tools: four that read (`team_task_plan`,
`team_coordination_overlaps`, `team_list_leases`, `team_integration_queue`) and
five that write (`team_claim_lease`, `team_renew_lease`, `team_release_lease`,
`team_declare_scope`, `team_enqueue_integration`).
`team_me` answers "who am I and what can I touch" (the project ids and
repository paths her roles grant); the task tools take a project id. See
[MCP](MCP.md).
`team_request_run` records a run; MCP cannot execute it. Bounded execution exists over
HTTP only, off by default ([bounded team execution](operations/TEAM_EXECUTION.md)). Member keys do not
carry `harness:write`, so `record_task_observation` and `record_task_lesson`
are refused for them.

## 6a. Shared memory goes through review

Once the Brain is a team, a decision or rule recorded by a member who is not a
team administrator (`POST /decisions`, `POST /rules`, MCP `record_decision`,
`record_rule`) becomes a pending **proposal** (HTTP 202) instead of approved
knowledge. Agents never receive a proposal in context packs, search or rule
checks. A Reviewer on the project who is not the author approves or rejects it
on the dashboard **Memory → Review** page, over MCP or over the API, with a
version check so two reviewers cannot both decide it. System-global proposals
need a team administrator. Readers cannot propose. Each member also has a
private personal tier of learnings that only they see, shared only through an
approved promotion. Details: [governed memory](MEMORY_REVIEW.md).

## Who am I, and what happened

- **`GET /team/me`** (MCP `team_me`, dashboard **Settings → Me**) returns the
  caller's principal, the team state (`solo` or `team`), whether they are a
  team administrator, their project roles with each project's effective
  permissions and repositories, and the scopes of the presented key. In solo
  state it reports the owner as the administrator of every repository.
- **`GET /team/activity`** (MCP `team_activity`, dashboard **Settings → Team
  activity**) is the shared log, newest first: decision and rule versions,
  learning, episode and skill changes, membership and project-role changes,
  execution grants and their revocation, invitations, team tasks and runs, and queued jobs with who queued them. It
  needs the `team:read` scope and takes `limit`, `offset`, `project_id`,
  `repository_id`, `actor_id`, `kind` (a family such as `decision` or a kind
  such as `decision.updated`, repeatable) and `since`.
  Members see events for the projects and repositories they can read plus
  global decisions and rules; membership, role, execution-grant and invitation events, and
  episode and skill changes, are for team administrators only; personal-tier
  learnings never appear. A project or
  repository filter the caller cannot read is refused with 403. Membership
  and job history starts with this release; earlier changes left no dated
  record.

## 7. Revoke

There are two different things to revoke, and they are not the same:

| Action | How | Effect |
|---|---|---|
| Revoke **membership** | `brain team member revoke --principal alice`, `DELETE /team/projects/members/{principal_id}`, or **Revoke** on the Team page | Ends her team membership and every project role, and revokes everything acting for her principal: her keys, OAuth access and refresh tokens bound to her or approved by her (personal-scope clients included), and the runs she started with their run credentials. Her next request gets 401, including the dashboard and MCP. |
| Revoke **one project role** | `brain team member revoke --principal alice --project core` | Removes only that project; membership and other roles stay. |
| Revoke **keys** | Dashboard **Settings → Access** (`/admin`): revoke the credential or disable the principal (`POST /admin/credentials/{id}/revoke`, `POST /admin/principals/{id}/disable`) | The key stops authenticating anything. Needs a team administrator. |
| Revoke an **invitation member** completely | **Revoke member** in the Team page's Invitations panel, `brain team invite revoke <invitation-id> --revoke-member`, or `POST /team/invitations/{id}/revoke` with `{"revoke_member": true}` | Membership, every project role and every credential of that principal. |

Revoking a membership also withdraws the member's pending memory proposals;
knowledge already approved stays, with them on record as its author or editor
([details](MEMORY_REVIEW.md#revoked-authors)).

Revoking one project role leaves her keys valid: they still work for her other
projects. A membership revocation also revokes the OAuth grants she approved
with personal scopes only, although such a client runs as its own
`oauth:<client>` service principal: its access and refresh tokens die with
her, so it cannot act as administrator after `brain team dissolve`. Other
principals' credentials are not touched.

Revoking the last live member does **not** return the Brain to solo. It stays
in team state: the shared key and any surviving credential have no implicit
authority. An operator returns it to solo with `brain team dissolve --yes`. In
solo state every authenticated key that is still valid acts as administrator.
Administrators cannot revoke their own membership over the API; an operator on
the server can.

## Team coordination (M3)

The first coordination slice lets several members and their agents split one
task without stepping on each other. Everything is recorded and checked by the
API; nothing here merges git branches or runs code by itself. All routes live
under `/team/projects/{project_id}` and go through the same authority checks
as tasks and runs.

**Subtasks and dependencies.** `POST .../tasks/{task_id}/subtasks` creates a
subtask of a task with `scope_paths` (repository-relative paths or globs such
as `src/api/**`) and `depends_on` (sibling subtask ids). A subtask never gets
more run permissions than its parent. `PUT .../tasks/{task_id}/plan` changes
the scope or dependencies with `expected_version`; a dependency cycle, a
dependency on the task itself or on a task outside the same parent is
refused with 409. `GET .../tasks/{task_id}/plan` shows each subtask as
`ready`, `waiting`, `running`, `done`, `cancelled` or `blocked` (a dependency
was cancelled). A run for a subtask can be requested only when every
dependency is done: the task is completed (integrating one of its runs through
the queue below completes it). The check runs again when a bounded execution
actually starts, so a run requested before its plan gained an unfinished
dependency is revoked before any step runs. `POST .../tasks/{task_id}/dispatch`
is the coordinator: it requests one run for every ready subtask that has no
active run yet, and a repeat call dispatches nothing new.

**Overlap warnings.** When a member declares a scope or takes a lease, the
Brain compares it with the scopes of other open tasks and with live leases in
the same repository. Overlaps come back as `warnings` in the response and are
recorded once per pair as `coordination.overlap` in the activity feed, visible
to members who can see that repository. They are warnings, not locks. The check
is conservative: a glob is compared by its fixed directory prefix, so it can warn
about paths that would not actually collide. A task and its own subtasks are
never flagged. `GET .../overlaps` lists the current overlaps. When a bounded
execution finishes, the files it changed against the task base (committed,
uncommitted and untracked) are compared the same way with the scopes of other
open tasks and with live leases; those warnings carry `source: changed_paths`
and are recorded once per pair too.

**Instruction versions.** When a run is requested, the Brain records which
decisions and rules applied to its repository and their versions.
`GET .../tasks/{task_id}/runs/{run_id}/instructions` reports `current` or
`superseded`, and names each instruction that was edited, retired or deleted
since the run started.

**Critical leases.** `POST .../leases` with `repository_id` and `resource` (a
path without globs, or a named resource such as `resource:db-migrations`) takes
an exclusive lease for `ttl_seconds` (10 to 3600, default 300). Overlapping
paths count as the same resource. Each lease carries a `fencing_token`; renew
and release (`.../leases/{id}/renew`, `.../release`) need the current token,
and only the holder can use it. An expired lease can be taken over, and the token
goes up, so a holder that wakes up late is refused. Revoking a member releases
their leases and the leases of runs they started. Readers cannot take leases.
A team administrator can force-release a stuck lease with a required reason
(`coordination.force_release_lease`; no HTTP route or MCP tool yet). The token
goes up, so the previous holder can no longer renew or release it, and the
release is recorded as `coordination.lease_force_released` in the activity feed
with the reason and the previous holder.

**Integration queue.** `POST .../integration` with a `run_id` queues a run that
reported a head revision. A Reviewer or Administrator other than the person who
started the run calls `.../integration/{id}/integrate` with `expected_version`
and, for the first entry, `current_base_sha`; later entries default to the
head of the last integrated entry. Entries are checked in order. The result is
recorded as one of:

| Status | Meaning |
|---|---|
| `integrated` | The run was built on the current base and its instructions are unchanged. Its task is marked completed (a `status_change` on the task ledger), so it counts as done. |
| `needs_rebase` | The run was built on an older base. Check it again after a rebase. |
| `stale_instructions` | A decision or rule it started with was changed before integration. |
| `rejected` | Rejected by a Reviewer or withdrawn by the member who queued it, or the run was revoked. |

Other final outcomes are also written to the task's ledger
(`integration.rejected`, for example) but leave the task open for another run.
The Brain records the decision and the base it checked against. It does not
run `git merge` or test the combined result. That stays with your normal
review and CI.

Read-only views: `brain team coord plan|overlaps|leases|queue --project <name>`
and the MCP tools `team_task_plan`, `team_coordination_overlaps`,
`team_list_leases` and `team_integration_queue`. Agents can also claim, renew
and release leases, declare a task's scope and queue a run for integration over
MCP (`team_claim_lease`, `team_renew_lease`, `team_release_lease`,
`team_declare_scope`, `team_enqueue_integration`). Those tools call the HTTP
routes above, with the same roles, fencing tokens and versions. Creating
subtasks and integrating or rejecting entries are HTTP-only.

The web dashboard's **Board** page (`/team/board`, under Settings) shows each
project you belong to. Tasks and subtasks sit in columns: blocked by
dependencies, ready, running, awaiting integration, needs rebase, stale
instructions and integrated. Each card shows its dependencies. The page also
lists live leases (holder, path, expiry, fencing token), overlap warnings and
the pending integration queue in order. It reads the routes above with your own
key. Its conflict view names the overlapping paths, the other task or lease
holder, and the superseded decision or rule versions, with links to Memory.
Each control appears only when your role allows it:

- release your own lease;
- request a fresh run of the task (Brain resolves the current instructions; the
  run key is shown once);
- integrate, if you are a Reviewer who did not queue the entry;
- reject, if you are a Reviewer, or withdraw, if you queued the entry.

A fresh run still starts from the task's declared base, so rebase in the agent
before you queue it again.

## Known limitations

These mirror the known gaps on the [status page](design/team-status.md),
which is the source of truth.

- **Without single sign-on the email is never checked.** "Verified" means the
  person presented an invitation token for an identity the administrator
  chose, or an operator added them on the server. Optional OIDC single sign-on
  (off by default) checks the invited email at your identity provider and lets
  members **Sign in with SSO**; one provider per Brain, see
  [Team single sign-on](operations/TEAM_SSO.md).
- **One API worker for native MCP.** Native MCP sessions need a single
  worker. The invitation-accept throttle (10 attempts per client IP per 5
  minutes) and the single sign-on throttle are shared through Redis, so they
  hold across API processes.
- **Dashboard invitees share one throttle unless the dashboard is trusted.**
  The `/invite` page calls the API from the dashboard server and passes the
  invitee's address on. The API believes it only from addresses listed in
  `BRAIN_TRUSTED_PROXY_IPS`; otherwise every invitee counts as the dashboard's
  IP and together they get 10 accept attempts per 5 minutes (see
  [Team invitations](operations/TEAM_INVITATIONS.md#abuse-limits)). Redeeming a
  token with the CLI counts against the invitee's own IP.
- **Invitations are not emailed.** The administrator copies the link and sends
  it; without single sign-on the email field is a note.
- **Execution is off by default.** Tasks and run requests are recorded with their
  provenance. The first M2 slice can run operator-allowlisted tools for a run once
  the operator enables it and an administrator grants a member; its subprocess
  backend is not containment and refuses egress
  ([bounded team execution](operations/TEAM_EXECUTION.md)).
- **Not deployed, not piloted.** Green CI is the only evidence. Production
  upgrade and restore, external identities and real multi-person use have not
  been exercised.
- Repository trust (`brain/workspace`) caps what anyone may do on a repository and
  membership (`brain/team`) decides who may use it; both are checked in one place
  (`authorize_repository`). A repository the workspace registry has disabled is
  denied to every member, administrators included, even when a project lists it.
  In team state, registering a repository (`POST /workspace/repositories`) needs a
  team administrator and links it to a team project: pass `project_id`, or attach
  it with `brain team project attach` first
  ([details](design/team-foundations.md#workspace-registry-and-team-membership-one-authority)).
- **Coordination does not merge.** The integration queue records decisions
  against a base you name; it does not run `git merge` or test the combined
  patch. Overlap detection compares paths (declared scopes, leases and the files an
execution changed), not code dependencies.

Backups of a team Brain have their own restore procedure, including the
restore fence and `brain team reconcile`:
[restoring a team Brain](runbooks/backup-restore.md#restoring-a-team-brain).
Security reporting scope: [SECURITY.md](../SECURITY.md#team-mode-experimental-preview).
