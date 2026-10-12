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
| `WEB_OPEN_MODE` | `true` lets anyone in while no login is configured. Use it only for local development. |
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
3. Nothing configured: the dashboard is **locked** (fail closed). Nobody can read data, the
   `/api/brain/*` proxy forwards nothing, and an anonymous visitor cannot set the first password.

**Open mode** (`WEB_OPEN_MODE=true`) restores the old behaviour for local development: with no
login configured anyone gets in and can set the first password from Settings. Turn it off
before the dashboard is reachable by anyone else.

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
