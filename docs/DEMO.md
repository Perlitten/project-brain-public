# Public demo site

`apps/web` renders **demo data** whenever no `BRAIN_API_URL`/`BRAIN_API_KEY`
is configured — the same mock corpus labeled "Demo data" throughout the UI.
A public demo is therefore a plain deployment with those variables unset.

## Vercel (recommended)

1. Import the repository, set **Root Directory** to `apps/web`.
2. Set only `NEXT_PUBLIC_DEMO_SITE=1` — this adds the
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
