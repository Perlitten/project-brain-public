# Dashboard request recovery

Date: 2026-10-14. This evidence covers local dashboard behavior, not hosted
deployment identity, semantic provider quality or overall release readiness.

The process-wide overview failure counter could suppress every repository for
30 seconds after three failures. Request-scoped React memoization now shares a
read only within its render, and each new request can observe recovery. Missing
endpoints, timeouts, HTTP failures and invalid responses retain distinct read
states. A healthy empty repository list remains different from an outage.

Repository and overview reads have two-second budgets; other GET reads default
to four seconds. Mutations/provider requests retain their existing budgets.
Layout reads start concurrently. Boot/status text does not claim online memory
when repository data is unavailable, and unknown freshness does not claim that
all indexed code is current. Credentials stay on the server.

| Verification | Result |
|---|---|
| Regression against previous source | Three failed overviews suppress the healthy fourth; the added test fails before the correction |
| Web unit tests | 42 pass |
| Type check / production build | Pass |
| Production integration | 2 pass; runtime bindings and isolated-demo protection retained |
| Actual Chromium comparison | 17 routes × desktop/mobile × before/after = 68 renders; no page errors, horizontal overflow or unnamed buttons |
| Keyboard interactions | Palette open/focus/Escape and mobile menu/Escape pass on both versions |
| Live synthetic failure fixture | Healthy fourth repository remains measured; failed repository recovers immediately on a later request |
| Timeout / HTTP error / invalid JSON | Explicit unavailable state; valid empty result remains empty |
| Fixture API key in browser HTML | Absent in all live cases |

In one local stalled-backend run, content-ready time fell from **10.6 seconds**
to **4.6 seconds**. This includes network-idle waiting and first-visit branding;
it is a synthetic comparison, not a production performance benchmark.

Captured records: [rendered routes and interactions](evidence/dashboard-recovery/rendered.json)
and [live failures and timings](evidence/dashboard-recovery/live.json).
Representative actual screenshots: [before failure](evidence/dashboard-recovery/before-wave.png),
[after recovery](evidence/dashboard-recovery/after-wave.png),
[desktop memory](evidence/dashboard-recovery/desktop-memory.png),
[mobile dashboard](evidence/dashboard-recovery/mobile-dashboard.png), and
[mobile navigation](evidence/dashboard-recovery/mobile-menu.png).

Demo captures use synthetic data and reduced motion. Every section was scrolled
into view before capture so offscreen canvases/entrance effects were rendered.
Live checks used an owned HTTP fixture, synthetic credentials and new compiled
server processes. External browser requests were blocked; no real model,
provider, operator API or user data was accessed. Existing releases are unchanged.
