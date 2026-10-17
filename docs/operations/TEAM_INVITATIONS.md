# Inviting team members

**Experimental preview, `main` only, not in v0.9.2; full storage only**
(`BRAIN_STORAGE_MODE=full`). The end-to-end path is in the
[team mode walkthrough](../TEAM.md); this page is the invitation reference. Invitations
turn the holder of a single-use token into a verified team member with a named
credential. Design and acceptance matrix: [team foundations](../design/team-foundations.md);
decision record: [ADR](../adr/team-collaboration.md).

## What "verified" means here

A member created by an invitation is verified because they presented an
unexpired, unrevoked, single-use token that an administrator or trusted operator
issued for an identity the issuer chose (principal name, optional email, project
roles). The member row records issuer `brain-invitation` and subject
`invitation:<id>`. This is **not** OIDC, SSO or email ownership proof: the email
is recorded for the operator and never checked. Whoever holds the token becomes
the member, so deliver it over a private channel.

## Lifecycle

States are derived, never stored:

| State | Meaning |
|---|---|
| `invited` | Issued, not yet redeemed, not expired |
| `expired` | Not redeemed before `expires_at` (default 72 h, at most 720 h) |
| `verified` | Redeemed; a member and a named credential exist |
| `active` | The issued credential has been used at least once |
| `revoked` | Withdrawn; for a redeemed invitation the member and credentials are revoked too |

Provenance kept on each invitation: `invited_by` (`principal:<id>` over the API,
`operator:cli` from the server CLI), `created_at`, `accepted_at`,
`accepted_principal_id`, `revoked_at`, `revoked_by`. The token is stored only as
a SHA-256 hash; it and the member credential are shown exactly once.

## Bootstrap: the first member (solo -> team)

While the Brain has no live member it is a team of one, and the shared
`PROJECT_BRAIN_API_KEY` acts as administrator. The first redemption flips it to
the team state, after which the shared key and keyless dev access are refused
(`Named credential required`). To make sure you are not locked out:

- The API refuses to create invitations in the solo state (HTTP 409). Bootstrap
  on the server, where the operator always keeps database access.
- The first invitation must grant team administration (`--admin`); a non-admin
  invitation is refused both at creation and at redemption while the team has
  no live member.

```bash
# On the server (inside the api container or with the server's .env loaded)
brain team init --name "My team"                 # once
brain team project add core                       # once per project
brain team invite create --admin --name owner --project core
# The token is printed once to stderr. Redeem it yourself:
brain team invite accept                          # prompts for the token (hidden)
# or from your workstation against the HTTPS API:
brain team invite accept --api-url https://brain.example.test
```

Store the printed `pbk_` member credential as your API key (for example
`X-API-Key` in your MCP client) before anything else uses the shared key.

## Inviting further members

As a team administrator (named credential with `team:admin`):

```bash
curl -sS -X POST "$BRAIN_API_URL/team/invitations" -H "X-API-Key: $ADMIN_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"name": "alice", "email": "alice@example.test", "projects": ["core"], "role": "contributor", "expires_hours": 48}'
# -> {"invitation": {...}, "token": "pbi_..."}   (Cache-Control: no-store)
```

or on the server: `brain team invite create --name alice --project core --role reader`.
Roles are the project roles (`reader`, `contributor`, `reviewer`) and only narrow
the project's policy; `is_admin` / `--admin` grants team administration.

The invitee redeems with `brain team invite accept --api-url <https URL>` or
`POST /team/invitations/accept` with `{"token": "pbi_..."}`. No credential is
needed for that call. The CLI accepts plain `http` only to loopback. A retry with
the same token after success returns the same member with `"credential": null`;
the credential cannot be recovered, only revoked and reissued.

An invitation dies with its inviter's administration: if the inviting
administrator was revoked or demoted, redemption fails with 409 and a new
invitation is needed.

## Listing and revoking

```bash
brain team invite list                                   # or GET /team/invitations
brain team invite revoke <invitation-id>                 # pending invitation
brain team invite revoke <invitation-id> --revoke-member # already redeemed
```

Over the API: `POST /team/invitations/{id}/revoke` with `{"revoke_member": true}`
for a redeemed invitation. Revoking a redeemed invitation without that flag is a
409 even if the member was already revoked elsewhere: the call never silently
revokes only the token. With the flag it revokes the team membership, every
project role and every credential of that principal. Administrators cannot
revoke their own membership this way. Repeating a revocation returns the
recorded state.

## Abuse limits

- Redemption is limited to 10 attempts per client IP per 5 minutes (HTTP 429),
  in-process per API instance like the web login limiter.
- Every rejected token (unknown, expired, revoked, malformed) gets the same
  403 answer; hashes are compared in constant time.
- Concurrent redemptions of one token are serialized by a row lock; exactly one
  member is created.

## Not provided

- No MCP tools: tokens and member credentials are shown once and must not end
  up in agent transcripts. Use the CLI or the API.
- No email delivery, self-registration, SSO/OIDC or web UI for invitations.
- The token is a bearer secret until redeemed or expired; revoke it if it may
  have leaked.
