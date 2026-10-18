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
`invitation:<id>`. Token-only acceptance is **not** email ownership proof: the
email is recorded for the operator and not checked. Whoever holds the token
becomes the member, so deliver it over a private channel.

When [single sign-on](TEAM_SSO.md) is enabled, an invitation that names an email
can instead be accepted through the identity provider
(`https://<dashboard>/auth/oidc/start?invitation=pbi_...`). Brain then requires
the provider to vouch for that exact email (case-insensitive, `email_verified`
true), records the invitation as verified via `oidc` (`verified_via`,
`verified_issuer`, `verified_subject` in the invitation JSON), uses the
provider's issuer and subject as the member's verification, and binds the
provider account so the member can later **Sign in with SSO**. With
`BRAIN_OIDC_REQUIRE_FOR_INVITATIONS=true`, token-only acceptance is refused
(HTTP 409) and invitations must name an email.

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

or on the server: `brain team invite create --name alice --project core --role reader`,
or from the dashboard: **Team → Invite a member**, which shows an invitation link
(`https://<dashboard>/invite#token=pbi_…`) once.
Roles are the project roles (`reader`, `contributor`, `reviewer`) and only narrow
the project's policy; `is_admin` / `--admin` grants team administration.

The invitee redeems by opening that link on the dashboard (`/invite`: it shows the
member key once and signs them in), with `brain team invite accept --api-url <https URL>`,
or with `POST /team/invitations/accept` and `{"token": "pbi_..."}`. When single
sign-on is on and the invitation names an email, they can instead open
`https://<dashboard>/auth/oidc/start?invitation=pbi_...` (see
[TEAM_SSO.md](TEAM_SSO.md)). Through the
dashboard every invitee reaches the API from the dashboard server, which passes
the invitee's address on in `X-Forwarded-For`; the API counts each invitee
separately only when `BRAIN_TRUSTED_PROXY_IPS` lists the dashboard (see
[Abuse limits](#abuse-limits)). No credential is needed for that call. The CLI accepts plain `http` only to loopback. A retry with
the same token after success returns the same member with `"credential": null`;
the credential cannot be recovered, only revoked and reissued.

An invitation dies with its inviter's administration: if the inviting
administrator was revoked or demoted, redemption fails with 409 and a new
invitation is needed.

## Listing and revoking

```bash
brain team invite list                                   # or GET /team/invitations, or the Team page
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

- Redemption is limited to 10 attempts per client IP per 5 minutes (HTTP 429
  with `Retry-After`).
- The client IP is the socket peer, unless the peer is listed in
  `BRAIN_TRUSTED_PROXY_IPS` (comma-separated addresses or CIDRs, empty by
  default). Then the API reads `X-Forwarded-For` from the right, skips every
  listed hop, and uses the first address that is not listed. List the web
  dashboard's address as the API sees it, plus any reverse proxy between them
  (for nginx in front of the API, its address too, since it appends the
  dashboard's address to the header). The dashboard sends the address its own
  front proxy reported (the rightmost `X-Forwarded-For` entry, else
  `X-Real-IP`; Vercel sets both), so run it behind such a proxy. Without the
  setting, every dashboard invitee shares the dashboard's one budget, and a
  forged header from an unlisted peer is ignored either way. The single sign-on
  throttle uses the same address. Uvicorn may already have rewritten the peer
  from the header for its own trusted hosts (`--forwarded-allow-ips`,
  default `127.0.0.1`); the API then starts from that address. The attempt log lives in Redis, so every API process
  and replica shares one limit. If Redis errors or does not answer within two
  seconds, that attempt is counted by a per-process fallback instead. Lite has
  no team; it keeps the window in memory, which is exact for its single
  process.
- Every rejected token (unknown, expired, revoked, malformed) gets the same
  403 answer; hashes are compared in constant time.
- Concurrent redemptions of one token are serialized by a row lock; exactly one
  member is created.

## Not provided

- No MCP tools: tokens and member credentials are shown once and must not end
  up in agent transcripts. Use the CLI or the API.
- No email delivery or self-registration. Single sign-on is optional and off by
  default ([TEAM_SSO.md](TEAM_SSO.md)); without it the email is not checked.
- The token is a bearer secret until redeemed or expired; revoke it if it may
  have leaked.
