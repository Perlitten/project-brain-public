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

Guard rails: the API refuses to create invitations while the Brain is solo
(HTTP 409), so the first one is always created on the server; and the first
invitation must be `--admin`. Details: [Inviting team members](operations/TEAM_INVITATIONS.md).

## 4. Invite a member

As an administrator, either on the server:

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

Alice redeems it from a machine with the `brain` CLI installed:

```bash
brain team invite accept --api-url https://brain.example.com
```

The CLI prompts for the token and prints her `pbk_` key once. Plain `http` is
accepted only to loopback. Without the CLI she can `POST /team/invitations/accept`
with `{"token": "pbi_..."}`; that call needs no credential.

Alternatives to invitations, both administrator-only: the dashboard **Team**
page adds a member and shows their key once, and
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

Both transports serve 17 tools: the personal ones plus `team_list_tasks`,
`team_create_task` and `team_request_run`, which take a project id (an
administrator gets it from `brain team project list`). See [MCP](MCP.md).
`team_request_run` records a run; MCP cannot execute it. Bounded execution exists over
HTTP only, off by default ([bounded team execution](operations/TEAM_EXECUTION.md)). Member keys do not
carry `harness:write`, so `record_task_observation` and `record_task_lesson`
are refused for them.

## 7. Revoke

There are two different things to revoke, and they are not the same:

| Action | How | Effect |
|---|---|---|
| Revoke **membership** | `brain team member revoke --principal alice`, `DELETE /team/projects/members/{principal_id}`, or **Revoke** on the Team page | Ends her team membership and every project role. She is signed out of the dashboard on her next request, and her key is refused for every project. **Her key stays a valid credential.** |
| Revoke **one project role** | `brain team member revoke --principal alice --project core` | Removes only that project; membership and other roles stay. |
| Revoke **keys** | Dashboard **Settings → Access** (`/admin`): revoke the credential or disable the principal (`POST /admin/credentials/{id}/revoke`, `POST /admin/principals/{id}/disable`) | The key stops authenticating anything. Needs a team administrator. |
| Revoke an **invitation member** completely | `brain team invite revoke <invitation-id> --revoke-member` (or `POST /team/invitations/{id}/revoke` with `{"revoke_member": true}`) | Membership, every project role and every credential of that principal. |

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
- **No invitation UI.** Invitations exist only in the CLI and the API. The
  dashboard Team page adds members directly.
- **One API process.** The invitation-accept throttle (10 attempts per client
  IP per 5 minutes) lives in process memory; run a single API process. Native
  MCP sessions need a single worker as well.
- **Execution is off by default.** Tasks and run requests are recorded with their
  provenance. The first M2 slice can run operator-allowlisted tools for a run once
  the operator enables it and an administrator grants a member; its subprocess
  backend is not containment and refuses egress
  ([bounded team execution](operations/TEAM_EXECUTION.md)).
- **Not deployed, not piloted.** Green CI is the only evidence. Production
  upgrade and restore, external identities and real multi-person use have not
  been exercised.
- Repository trust (`brain/workspace`) and human membership (`brain/team`) are
  still two separate models.

Backups of a team Brain have their own restore procedure, including the
restore fence and `brain team reconcile`:
[restoring a team Brain](runbooks/backup-restore.md#restoring-a-team-brain).
Security reporting scope: [SECURITY.md](../SECURITY.md#team-mode-experimental-preview).
