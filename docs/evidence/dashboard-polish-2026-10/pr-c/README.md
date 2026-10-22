# PR C — Vercel preview evidence

Public synthetic demo, not the authenticated live dashboard or Contabo API. Before: #186 build `03acca2`. After: #187 build `04a6b17`. Manifests record the preview URLs, titles and headings. Captures wait for streamed content and hydrated navigation, at 1440 × 1100 with reduced motion. The browser ignored certificate errors for its session because Chromium lacks the execution proxy CA; curl verified these public endpoints normally. No private project data is included.

| Screen | Before | After |
| --- | --- | --- |
| memory | [before](before/memory.png) | [after](after/memory.png) |
| findings | [before](before/findings.png) | [after](after/findings.png) |
| activity | [before](before/activity.png) | [after](after/activity.png) |
| settings | [before](before/settings.png) | [after](after/settings.png) |

The demo is read-only: its disabled mutation buttons do not prove production API functionality. The additional local browser captures use a synthetic loopback API: [Current memory](local-current-memory.png), [reasoned dismissal](local-dismiss-reason.png), [project/query/trace presentation](local-activity-query.png). Nine browser assertions pass, including Open code, Dismiss refresh, escaped query text and trace navigation. Separate SQL/HTTP regression tests in `tests/test_dashboard_content_actions.py` verify real authorization, source isolation, stale-version rejection, identity provenance, retries, query opt-in and memory pagination.

Historical request queries are unavailable. Query capture stays off unless the operator opts in. Historical module-only findings cannot open source without explicit repository/file evidence. Duplicate grouping covers the loaded page; findings remain global in the current schema, and repository-scoped listing continues to fail closed. No server or Vercel settings were changed.
