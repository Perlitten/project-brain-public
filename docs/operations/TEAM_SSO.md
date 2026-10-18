# Team single sign-on (OIDC)

**Experimental preview, `main` only, not in v0.9.2; full storage only**
(`BRAIN_STORAGE_MODE=full`). Off by default. This page is the reference for
signing team members in through your organization's identity provider (IdP)
with OpenID Connect. The rest of team mode is in the
[team mode walkthrough](../TEAM.md) and [inviting team members](TEAM_INVITATIONS.md).

With single sign-on on:

- an invitation that names an email can be accepted by signing in at the IdP;
  the IdP must vouch for that exact email (`email_verified: true`), and the
  member is then bound to the IdP account;
- a bound member signs in to the dashboard with **Sign in with SSO** instead of
  pasting a `pbk_` member key;
- the operator can require SSO for every invitation (no token-only acceptance).

Nothing is provisioned automatically: an IdP account opens a session only when
it is bound to an active team member, either by accepting an invitation through
SSO or by an operator binding (below).

## Configuration

Set these on the **API** (the dashboard needs nothing new beyond `BRAIN_API_URL`
and `BRAIN_API_KEY`):

| Variable | Default | Meaning |
|---|---|---|
| `BRAIN_OIDC_ENABLED` | `false` | Turns SSO on. Everything below is ignored while off. |
| `BRAIN_OIDC_ISSUER` | — | Issuer URL. Must publish `<issuer>/.well-known/openid-configuration`, and the discovered `issuer` must equal it. `https` (plain `http` only for loopback). |
| `BRAIN_OIDC_CLIENT_ID` | — | Client id registered at the IdP. |
| `BRAIN_OIDC_CLIENT_SECRET` | empty | Client secret for a confidential client (`client_secret_basic`, or `client_secret_post` when that is all the IdP offers). Leave empty for a public client; PKCE is always used. Keep it in `.env`, never in the repository. |
| `BRAIN_OIDC_REDIRECT_URI` | `BRAIN_WEB_URL` + `/auth/oidc/callback` | Must be registered at the IdP exactly. |
| `BRAIN_OIDC_SCOPES` | `openid email profile` | Must include `openid`; the ID token must carry `email` and `email_verified`. |
| `BRAIN_OIDC_ALLOWED_EMAIL_DOMAINS` | empty | Comma-separated domains (for example `example.com,example.org`). Empty allows any domain the IdP vouches for. |
| `BRAIN_OIDC_REQUIRE_FOR_INVITATIONS` | `false` | Refuse token-only acceptance (`brain team invite accept`, `POST /team/invitations/accept`) and refuse to create invitations without an email. |

The API refuses to start when SSO is enabled without an issuer, a client id or a
redirect URI, or with non-https URLs.

At the IdP, register a web application with:

- redirect URI `https://<dashboard host>/auth/oidc/callback`;
- grant type authorization code with PKCE (S256);
- an ID token signed with an asymmetric algorithm (RS*, PS*, ES* or EdDSA);
- the `email` claim and a truthful `email_verified` claim.

`GET /team/oidc/config` answers `{"enabled": …, "required_for_invitations": …}`
without credentials; the dashboard uses it to show the button.

## Inviting a member through SSO

Create the invitation with the invitee's email, as usual:

```bash
brain team invite create --name alice --email alice@example.com --project core
# or POST /team/invitations with {"name": "alice", "email": "alice@example.com", ...}
```

Send the invitee this link instead of (or together with) the token:

```
https://<dashboard host>/auth/oidc/start?invitation=pbi_...
```

The dashboard asks Brain for an authorization request, sends the browser to the
IdP (with the invitation email as `login_hint`) and, after sign-in, accepts the
invitation when the ID token's verified email equals the invitation email
(case-insensitive, exact; no lookalike or subaddress matching). The invitation
is recorded as verified through `oidc` with the IdP issuer and subject, the
team member's verification issuer and subject become the IdP's, and the member
lands on the dashboard already signed in. Refusals are shown on the sign-in
page: email mismatch, unverified email, an invitation without email, or an IdP
account that is already linked to another member.

The token stays a bearer secret until it is redeemed: whoever holds the link can
start the flow, but only the person who controls the invited email at the IdP
can finish it. With `BRAIN_OIDC_REQUIRE_FOR_INVITATIONS=true` that is the only
way in.

`POST /team/oidc/start` also accepts the token directly
(`{"binding": "...", "invitation": "pbi_..."}`) for another front end; see
"Protocol" below.

## Binding existing members

Members who joined before SSO was turned on (or by token) are bound by the
operator on the server. No email is checked: the operator vouches for the
mapping. The subject is the IdP's stable `sub` for that person (find it in the
IdP's admin console).

```bash
brain team member sso-bind --principal alice --subject 00u1abcd... [--issuer https://idp.example.com] [--email alice@example.com]
brain team member sso-list
brain team member sso-unbind --principal alice [--issuer https://idp.example.com]
```

`--issuer` defaults to `BRAIN_OIDC_ISSUER`. A member has at most one binding per
issuer, and an IdP account is bound to at most one member. `sso-unbind` also
revokes the dashboard credential that binding's latest sign-in issued, so an
open SSO session ends at once. Binding (by the operator or by accepting an
invitation through SSO) and unbinding are recorded in the team activity feed as
`sso.bound` and `sso.unbound`, visible to team administrators.

## Signing in

On the dashboard sign-in page a team member chooses **Sign in with SSO**
(`/auth/oidc/start?next=<path>`). After the IdP round trip Brain maps the
verified issuer and subject to the bound member and issues a seven-day `pbk_`
credential with exactly the scopes of that member's dashboard key (members:
the usual read/write scopes; administrators also `team:admin`). The dashboard
seals it into the same httpOnly `pb_member` cookie as a pasted member key, so
the session model is identical: every request is re-checked with Brain, and
revoking the member ends the session at once.

A member holds one live SSO credential per binding: Brain remembers the
credential each sign-in issued and revokes it on the next sign-in (the key an
invitation accepted through SSO issues counts as the first session), so signing
in on a second browser signs the first one out. Signing out
of the dashboard (`/__logout`) calls `POST /team/oidc/logout` with the member's
key, which revokes it when it is that member's current SSO session; a member key
pasted at sign-in, or one used by the CLI or MCP clients, is not touched.

The `/invite` page offers **Sign in with SSO** next to the token form when SSO
is on. It POSTs the token from the field to `/auth/oidc/start` as `invitation`,
so the token stays out of URLs.

Refused: an unknown IdP account (not bound), a revoked or disabled member, an
unverified member, and a database that is fenced after a
restore.

## Protocol and security

- Authorization-code flow with PKCE S256, a random `state` and a random `nonce`
  per attempt. The authorization request is single-use and expires after ten
  minutes.
- Browser binding: the dashboard generates a random 256-bit binding secret and
  keeps it, with `state`, in an httpOnly, `SameSite=Lax` cookie scoped to
  `/auth/oidc` (`pb_oidc`, ten minutes, `Secure` on https). Brain stores only
  SHA-256 hashes of `state` and the binding and redeems a code only together
  with the binding, so a code leaked from the redirect (proxy logs, history) is
  useless in another browser. The dashboard compares `state` with its cookie
  before calling Brain.
- The request is spent before the code exchange, so a code is tried at most
  once.
- ID token validation (PyJWT): signature against the IdP's JWKS (refreshed once
  on an unknown `kid`), asymmetric algorithms only (`none` and HS* are refused),
  `iss`, `aud` (and `azp` when there are several audiences), `exp` and `iat`
  with 60 seconds of clock skew, `nonce`, `email_verified` true (the boolean, or
  the string `"true"` that some IdPs send), and the allowed domains. Discovery must return the configured issuer and https
  endpoints on any host.
- `POST /team/oidc/start` and `/team/oidc/callback` share a throttle of 30
  attempts per 5 minutes per client address (see `BRAIN_TRUSTED_PROXY_IPS` in
  [TEAM_INVITATIONS.md](TEAM_INVITATIONS.md) for how the address is found
  behind the dashboard), kept in Redis so every API process
  counts against the same limit (`429` with `Retry-After`; if Redis fails, the
  limit falls back to each process). They carry no credential and
  answer `Cache-Control: no-store`. Invitation tokens and codes travel in JSON
  bodies to the API, never in its URLs.

Library choice: PyJWT with `cryptography` (`pyjwt[crypto]`) was already locked
as a dependency of the MCP SDK and is now declared directly. It covers JWKS key
parsing and registered-claim validation; the rest of the flow is four HTTP
calls with httpx. A full OIDC client library (Authlib) would add a large
dependency for little extra, and keeping the checks in `brain/team/oidc.py`
makes each one visible and tested against a fake IdP
(`tests/test_team_oidc.py`).

## Limitations

- One identity provider per Brain.
- Unless `BRAIN_TRUSTED_PROXY_IPS` lists the dashboard's address, the throttle
  is keyed by the socket peer and all dashboard users share the dashboard's
  address: a burst from one person can delay others for a few minutes.
- An invitation token put in the query of a `/auth/oidc/start` link by hand (as
  Brain's "use single sign-on" refusal suggests) can appear in dashboard access
  logs and browser history until it is redeemed (the dashboard redirects
  immediately with `Referrer-Policy: no-referrer`). The `/invite` page POSTs it
  instead. Keep the expiry short.
- A browser that is closed without signing out leaves its SSO credential live
  until the next sign-in rotates it or its seven days run out. Revoke the
  member, or their credentials, to cut access sooner.
- An IdP account stays bound to a member after that member is revoked. To let
  the same person join again through a new invitation, unbind it first.
- Login CSRF: an attacker who can plant cookies for the dashboard's host (for
  example from a sibling subdomain) could sign a victim's browser in as the
  attacker's member. Serve the dashboard on its own host.
- Email changes at the IdP do not matter after binding: sign-in uses the
  issuer and subject only. The latest email is recorded for the operator.

Related: [SECURITY.md](../../SECURITY.md) (scope),
[inviting team members](TEAM_INVITATIONS.md).
