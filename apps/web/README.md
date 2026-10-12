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

## Development

Requires Node.js 20.9 or newer.

```bash
npm ci && npm run dev     # http://localhost:3100
npm test                  # unit and regression tests (node:test)
npm run typecheck         # tsc --noEmit
npm run build             # production build
```

## Deploy (Vercel)

Set the project root to `apps/web` and add the variables above in the Vercel project settings
(at least `BRAIN_API_URL`, `BRAIN_API_KEY` and a sign-in). Never set `WEB_OPEN_MODE` on a
public deployment. The region is pinned in `vercel.json`.
