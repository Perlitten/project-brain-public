# PR A — Vercel preview evidence

These screenshots show the public **synthetic demo**, not the private live dashboard or Contabo API. Private live access returns HTTP 401; its preview requires Vercel SSO. This evidence cannot establish live backend deployment or latency.

Before: `f5e39f569a6765f1e6ab29089df3da27a00c461b` (UI identical to merged #183). After: `34340d49408368ee7d4f6e80df7d66b48d087b8c` (#185). The manifests record exact preview URLs, headings and titles. Captured at 1440 × 1100 with reduced motion and no interactions that mutate data. The browser ignored certificate errors for this session because Chromium does not trust the execution environment's HTTPS proxy CA; curl verified these public endpoints with its normal trust store.

| Screen | Before | After |
| --- | --- | --- |
| dashboard | [before](before/dashboard.png) | [after](after/dashboard.png) |
| projects | [before](before/projects.png) | [after](after/projects.png) |
| help | [before](before/help.png) | [after](after/help.png) |
| activity | [before](before/activity.png) | [after](after/activity.png) |
| memory | [before](before/memory.png) | [after](after/memory.png) |
| index-runs | [before](before/index-runs.png) | [after](after/index-runs.png) |
| code-map | [before](before/code-map.png) | [after](after/code-map.png) |
| settings | [before](before/settings.png) | [after](after/settings.png) |
| agent-tools | [before](before/agent-tools.png) | [after](after/agent-tools.png) |
