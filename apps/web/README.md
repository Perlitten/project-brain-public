# Project Brain web dashboard

A Next.js 16 dashboard that shows what Project Brain knows about your code: indexed
projects, memory, quality, runs and settings. It talks to the Brain HTTP API from the
server only; the API key never reaches the browser.

## Environment

All variables are read by server code only. Copy `.env.example` to `.env.local` for local work.

| Variable | Purpose |
|---|---|
| `BRAIN_API_URL` | Base URL of the Brain API (a path prefix such as `/brain` is kept). |
| `BRAIN_API_KEY` | API key, sent as `X-API-Key` from the server. |
| `WEB_BASIC_AUTH` | Optional fixed sign-in, `user:password`. When set it wins and can't be changed from the dashboard. |
| `WEB_HOST` | Address `npm run dev` / `npm run start` listen on. Default `127.0.0.1` (this machine only). |
| `WEB_OPEN_MODE` | `true` lets anyone in while no login is configured, on any address; `false` locks a loopback dashboard too. Unset: open only on loopback. |
| `BRAIN_WEB_ACTIONS` | `0` turns dashboard actions off everywhere. `1` turns them on even without a password, so use it only behind your own SSO. |
| `BRAIN_PUBLIC_API_URL` | Optional API URL shown in the agent setup snippets. Defaults to `BRAIN_API_URL`. |
| `BRAIN_TIMEZONE` | Optional IANA time zone for times shown on screen. Defaults to UTC. |

## Demo and live mode

- **Demo**: neither `BRAIN_API_URL` nor `BRAIN_API_KEY` is set. The dashboard shows sample
  data, is public, and may be indexed by search engines.
- **Live**: both are set. The dashboard is private: `robots.txt` disallows everything and pages
  carry `noindex`.
- **Misconfigured**: only one of the two is set, `WEB_BASIC_AUTH` is not `user:password`, or the
  Brain API reports only one of `WEB_LOGIN_USER` / `WEB_LOGIN_PASSWORD_HASH`. The dashboard
  stays locked with HTTP 503, logs the reason on the server, and shows it on the sign-in page.

## Sign-in (live mode)

Who may enter, in order:

1. `WEB_BASIC_AUTH`.
2. The login kept by the Brain API (set on Settings, Access, Dashboard sign-in, or with
   `PUT /api/web-login`).
3. Nothing configured:
   - **on loopback, opened from this machine** (the default: `npm run dev` and `npm run start`
     listen on `127.0.0.1`, and the request's `Host` is `localhost`/`127.x`/`[::1]`), the
     dashboard opens without a password, shows a "No password" chip in the top bar, and lets
     you set one from Settings. Nobody else can reach it.
   - **on any other address** (`WEB_HOST=0.0.0.0`, Vercel, a reverse proxy), the dashboard is
     **locked** (fail closed). Nobody can read data, the `/api/brain/*` proxy forwards nothing,
     and an anonymous visitor cannot set the first password.

`WEB_OPEN_MODE=true` forces open mode on any address; `WEB_OPEN_MODE=false` locks a loopback
dashboard too. A reverse proxy on the same host forwards its public `Host` header, so a
dashboard published that way stays locked until a sign-in exists (keep the public `Host`;
do not rewrite it to `localhost`).

Scripts can authenticate with HTTP Basic (`curl -u user:password`). Refusals from the proxy and
`/api/brain/*` are JSON `{ "error", "detail" }` with status 401 or 503.

### Team sign-in

> **Experimental preview, `main` only, not in v0.9.2.** The end-to-end operator path (invite,
> sign-in, MCP, revoke) and the known limitations are in the
> [team mode walkthrough](../../docs/TEAM.md).

Once the Brain has a verified team member it refuses the shared key the dashboard runs with
(`Named credential required`). The dashboard notices that refusal and switches to team sign-in;
`WEB_BASIC_AUTH` and the owner login stop applying until the last member is gone.

- Each member signs in with their **own member key** (a `pbk_` credential). The dashboard checks
  it with `GET /api/web-session` and keeps it in the `pb_member` cookie: `httpOnly`, `SameSite=Lax`,
  `Secure` over https, sealed with AES-256-GCM under a key derived from `BRAIN_API_KEY`, valid for
  7 days. Browser JavaScript can neither read nor forge it. Rotating `BRAIN_API_KEY` signs every
  member out.
- Every page render, `/api/brain/*` call and dashboard action sends that member's key, never the
  server key, so Brain authorizes each call as the member. A request without a valid member cookie
  forwards nothing. Member responses skip the data cache.
- Each request re-checks the key with Brain, so revoking a member or their key takes effect on the
  next request. The stale cookie is then cleared. Revoking the membership locks the member out of
  the dashboard but leaves the key itself valid; revoke the key under **Settings → Access** to stop
  their agents as well. **Sign out** in the top bar deletes the cookie.
- **Team** (sidebar) shows who you are. A team admin can add members, issue a member key (shown
  once), set project roles and revoke members through the `/team` API. Brain still checks every call.
- A team admin can also **invite** someone from the Team page: the dashboard creates the invitation
  with `POST /team/invitations` and shows a link `https://<dashboard>/invite#token=pbi_…` once. The
  Invitations panel lists each invitation's state and revokes it (`POST /team/invitations/{id}/revoke`).
- **`/invite`** is public. It is a standalone page served by `proxy.ts`, like the sign-in page, so
  no dashboard data or layout reaches someone who isn't signed in. The token travels in the URL
  fragment, which browsers never send; the page moves it into the form and clears the address bar.
  The POST must come from the dashboard's own origin. The dashboard calls
  `POST /team/invitations/accept` **without** its server key, shows the new member key once, then
  signs the invitee in through the same `GET /api/web-session` check and sealed `pb_member` cookie as
  the sign-in form. Responses are `no-store`, `no-referrer` and cannot be framed. Unknown, expired and
  revoked tokens get one message, matching Brain. The throttle and an outage get their own
  messages, and an outage says the token wasn't used. The page is off with
  `BRAIN_WEB_ACTIONS=0`, in demo mode and when the dashboard is misconfigured. Extra sign-in
  buttons (for example SSO) can be passed to `invitePage({ alternatives })` in `lib/invite-page.ts`.

Bootstrap the first admin on the Brain server, then sign in with their key:

```bash
brain team init --name "My team"
brain team member add --principal alice --admin --mint-key   # prints the key once
```

Redeeming an `--admin` invitation works too: run `brain team invite create --admin`, then either
`brain team invite accept` or paste the token at `/invite` on the dashboard, which also works while
the dashboard is still in solo sign-in. Keys from the Team page, from `--mint-key` and from invitations carry the same
scopes: the team scopes, the dashboard read scopes and `core:write` for search, context and ask;
admins also get `principals:read`/`principals:write`, `setup:write` and `operations:write`. A
member key minted before PR #124 carries team scopes only and cannot open the dashboard; issue a
new one. If `BRAIN_API_KEY` is itself a member's `pbk_` key, Brain
accepts it, so the dashboard stays in solo mode and acts as that member. Use the shared server key
or a service credential instead.

## Development

Requires Node.js 20.9 or newer.

```bash
npm ci && npm run dev     # http://localhost:3100
npm test                  # unit and regression tests (node:test)
npm run typecheck         # tsc --noEmit
npm run build             # production build
npm run test:production   # runtime demo/live binding regression (after build)
```

## Deploy (Vercel)

Set the project root to `apps/web` and add the variables above in the Vercel project settings
(at least `BRAIN_API_URL`, `BRAIN_API_KEY` and a sign-in). Never set `WEB_OPEN_MODE` on a
public deployment. The region is pinned in `vercel.json`.
