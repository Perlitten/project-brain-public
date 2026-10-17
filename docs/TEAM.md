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
  from another machine ([Deployment](operations/DEPLOYMENT.md)).
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

Both transports serve 19 tools: the personal ones plus `team_me`,
`team_activity`, `team_list_tasks`, `team_create_task` and `team_request_run`.
`team_me` answers "who am I and what can I touch" (the project ids and
repository paths her roles grant); the task tools take a project id. See
[MCP](MCP.md).
`team_request_run` records a run; nothing executes it yet. Member keys do not
carry `harness:write`, so `record_task_observation` and `record_task_lesson`
are refused for them.

## Who am I, and what happened

- **`GET /team/me`** (MCP `team_me`, dashboard **Settings → Me**) returns the
  caller's principal, the team state (`solo` or `team`), whether they are a
  team administrator, their project roles with each project's effective
  permissions and repositories, and the scopes of the presented key. In solo
  state it reports the owner as the administrator of every repository.
- **`GET /team/activity`** (MCP `team_activity`, dashboard **Settings → Team
  activity**) is the shared log, newest first: decision and rule versions,
  learning, episode and skill changes, membership and project-role changes,
  invitations, team tasks and runs, and queued jobs with who queued them. It
  needs the `team:read` scope and takes `limit`, `offset`, `project_id`,
  `repository_id`, `actor_id`, `kind` (a family such as `decision` or a kind
  such as `decision.updated`, repeatable) and `since`.
  Members see events for the projects and repositories they can read plus
  global decisions and rules; membership, role and invitation events, and
  episode and skill changes, are for team administrators only. A project or
  repository filter the caller cannot read is refused with 403. Membership
  and job history starts with this release; earlier changes left no dated
  record.

## 7. Revoke

There are two different things to revoke, and they are not the same:

| Action | How | Effect |
|---|---|---|
| Revoke **membership** | `brain team member revoke --principal alice`, `DELETE /team/projects/members/{principal_id}`, or **Revoke** on the Team page | Ends her team membership and every project role. She is signed out of the dashboard on her next request, and her key is refused for every project. **Her key stays a valid credential.** |
| Revoke **one project role** | `brain team member revoke --principal alice --project core` | Removes only that project; membership and other roles stay. |
| Revoke **keys** | Dashboard **Settings → Access** (`/admin`): revoke the credential or disable the principal (`POST /admin/credentials/{id}/revoke`, `POST /admin/principals/{id}/disable`) | The key stops authenticating anything. Needs a team administrator. |
| Revoke an **invitation member** completely | **Revoke member** in the Team page's Invitations panel, `brain team invite revoke <invitation-id> --revoke-member`, or `POST /team/invitations/{id}/revoke` with `{"revoke_member": true}` | Membership, every project role and every credential of that principal. |

When someone leaves, revoke the membership **and** their keys. A key left
alive after a membership revocation becomes fully usable again if the person is
re-added with `member add`, or if the Brain returns to solo state.

The Brain returns to solo state when its last live member is revoked. In solo
state every authenticated key, including old member keys, acts as
administrator. Administrators cannot revoke their own membership over the API;
an operator on the server can, so revoke credentials first if you ever wind a
team down.

## Known limitations

These mirror the known gaps on the [status page](design/team-status.md),
which is the source of truth.

- **No SSO or OIDC, and the email is never checked.** "Verified" means the
  person presented an invitation token for an identity the administrator
  chose, or an operator added them on the server.
- **One API worker for native MCP.** Native MCP sessions need a single
  worker. The invitation-accept throttle (10 attempts per client IP per 5
  minutes) is shared through Redis, so it holds across API processes.
- **Dashboard invitees share one throttle.** The `/invite` page calls the API
  from the dashboard server, so the API sees every invitee as the dashboard's
  IP: together they get 10 accept attempts per 5 minutes. Redeeming a token
  with the CLI counts against the invitee's own IP instead.
- **Invitations are not emailed.** The administrator copies the link and sends
  it; the email field is a note.
- **Execution is denied.** Tasks and run requests are recorded with their
  provenance; no executor exists (M2 has not started).
- **Not deployed, not piloted.** Green CI is the only evidence. Production
  upgrade and restore, external identities and real multi-person use have not
  been exercised.
- Repository trust (`brain/workspace`) and human membership (`brain/team`) are
  still two separate models.

Backups of a team Brain have their own restore procedure, including the
restore fence and `brain team reconcile`:
[restoring a team Brain](runbooks/backup-restore.md#restoring-a-team-brain).
Security reporting scope: [SECURITY.md](../SECURITY.md#team-mode-experimental-preview).
