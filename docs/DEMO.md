# Public demo site

The hosted demo is **not independently verified for the public launch**. Launch
announcements must omit its URL until the checklist below is completed from an
external, unauthenticated browser. Local screenshots and mock metrics do not
establish hosted availability, real usage, coding quality or efficiency gains.

## External verification required before promotion

Use the maintainer-supplied candidate URL for this checklist. It is a
verification target, not a supported launch link yet; do not add an unverified
deployment hostname to public launch material.

1. Open it signed out, on desktop and a phone; confirm HTTPS and successful load.
2. Visit Dashboard, Projects/Code map, Memory, Context packs and Setup; check
   navigation, mobile menu, console/runtime errors, missing assets and error states.
3. Confirm every synthetic-data surface is labeled **Demo data**; do not present
   example counts or quality scores as measured product results.
4. Confirm no live API binding, real repository data or credentials are present.
   Demo actions must refuse mutations; direct `/api/brain/*` requests must not
   reach a privileged backend. Do not test destructive operations on a live API.
5. Follow the install CTA and confirm it reaches
   `https://github.com/Perlitten/project-brain-public#readme`, then follow the
   public source quickstart and security/release links.
6. Record date, deployed revision, desktop/mobile screenshots and outcomes.
   Promote the URL only after all checks pass; otherwise keep it out of launch
   material and report the exact failure.

`apps/web` renders **demo data** whenever no `BRAIN_API_URL`/`BRAIN_API_KEY`
is configured — the same mock corpus labeled "Demo data" throughout the UI.
A public demo is therefore a plain deployment with those variables unset.

## Vercel (recommended)

1. Import the repository, set **Root Directory** to `apps/web`.
2. Set only `NEXT_PUBLIC_DEMO_SITE=1` **at build time and runtime** — this adds the
   "Live demo with example data → install your own" banner in the rail that
   links back to the README quickstart.
3. Deploy. With no API env, every page serves demo data server-side; downloads,
   pack/report readers and robots.txt (allow-all in pure demo) all work.

## Why not GitHub Pages / static export

The app uses Server Actions and route handlers (the `/api/brain/*` proxy,
artifact downloads, env-evaluated `robots.ts`) — Next.js `output: "export"`
does not support them. A static export would need a feature-reduced build
subset; until that's worth maintaining, the demo is a Node host (Vercel or
any `npm run start`).
