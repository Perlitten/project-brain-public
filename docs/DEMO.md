# Synthetic product demonstration

The current hosted operator dashboard requires authentication. It is not a
public synthetic demonstration and is not linked from the product landing page.
Its protection remains intact. Current, genuine interface captures are available
in the [README](../README.md) and [showcase assets](assets/).

## A separate, safe deployment

Use a separate Vercel project, with Root Directory `apps/web`. Give it an
independent public URL and no connection to the operator's environment.
The reviewed configuration is [`.env.demo.example`](../apps/web/.env.demo.example):

| Variable | Synthetic deployment value |
|---|---|
| `BRAIN_DEMO_MODE` | `1` |
| `NEXT_PUBLIC_DEMO_SITE` | `1` at build time and runtime |
| `BRAIN_API_URL`, `BRAIN_API_KEY` | Unset |
| `WEB_BASIC_AUTH` | Unset |
| `BRAIN_WEB_ACTIONS` | `0` |

`BRAIN_DEMO_MODE` is a server-side safety guard. If a backend binding or operator
sign-in credential is present, requests fail closed with 503 before any backend
read or mutation. It never removes authentication from an operator deployment.
With both API bindings absent, the existing data adapters serve synthetic
examples, which are visibly labeled on every page, including mobile. Server
actions refuse mutations, and the API bridge has no upstream credentials.

For a local walkthrough, from `apps/web` with no backend or sign-in bindings:

```bash
npm ci
BRAIN_DEMO_MODE=1 NEXT_PUBLIC_DEMO_SITE=1 npm run build
BRAIN_DEMO_MODE=1 NEXT_PUBLIC_DEMO_SITE=1 npm run start
```

The server listens on loopback. A public deployment additionally needs the host's
account/project configuration to allow anonymous access. Vercel SSO protection
and application authentication are separate checks; a READY deployment alone
does not establish anonymous availability.

## Acceptance before promotion

1. Confirm the separate project's environment contains no backend credentials,
   private repository bindings or operator secrets, in any deployment scope.
2. Open its URL signed out on desktop and mobile. Every primary route must return
   200 and show the synthetic-data notice; screenshots must retain that label.
3. Check Dashboard, Projects, Code map, Memory, Context packs, Indexing, Setup,
   Quality and Settings, including filtering, empty results and navigation.
4. Check keyboard focus, the mobile menu, reduced-motion settings, asset loading
   and browser console errors.
5. Confirm demo controls refuse privileged actions and `/api/brain/setup/status`
   returns 503 without contacting a backend. Inspect the visible HTML for
   credentials. An operator deployment with live data must retain sign-in.
6. Follow the install CTA to the public repository and complete the recommended
   [source installation](GETTING_STARTED.md).
7. Record deployed commit, anonymous responses, desktop/mobile captures and the
   configuration review. Add the URL to GitHub only after these checks pass.

## Current external access limitation

This execution environment has no Vercel token, project binding, CLI session or
outbound deployment identity. The existing integration reports a completed
production deployment, but its anonymous response is application-level 401.
GitHub repository-settings writes also return `Resource not accessible by
integration`. Content, pull requests and release operations have separate usable
permissions; those do not grant Vercel project administration.

A Vercel account owner must grant access to create/configure the separate project
and review its SSO/environment settings. No production environment was inspected
for secret values or modified. Until that access exists, screenshots and the
local synthetic walkthrough are the working example; the protected domain is
excluded from promotional links.

## Hosting requirements

Use Vercel or a Node host running `npm run start`. The application uses Server
Actions and route handlers, so Next.js static export does not support the complete
experience. A feature-reduced static clone would duplicate implementation and is
not maintained as a substitute.
