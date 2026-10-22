# PR B — Vercel preview evidence

Public synthetic demo, not the private live dashboard or Contabo API. Before: #185 build `5a61c5b` (UI identical after its later CI merge). After: #186 build `7e456d5`. Capture manifests contain the exact Vercel URLs, titles and headings. The later wildcard-message fix affects the rotation dialog; local Chromium separately verifies that submitting wildcard scopes is blocked until an explicit replacement is selected, and that an older dashboard key retains its own scopes. No credential was minted or revoked by that browser check.

Screens captured at 1440 × 1100 with reduced motion. Chromium ignored HTTPS certificate errors for this browser session because it lacks the execution environment's proxy CA; curl verified the preview endpoints normally. No private live credentials or project data appear here.

| Screen | Before | After |
| --- | --- | --- |
| dashboard | [before](before/dashboard.png) | [after](after/dashboard.png) |
| projects | [before](before/projects.png) | [after](after/projects.png) |
| help | [before](before/help.png) | [after](after/help.png) |
| activity | [before](before/activity.png) | [after](after/activity.png) |
| quality | [before](before/quality.png) | [after](after/quality.png) |
| settings | [before](before/settings.png) | [after](after/settings.png) |
| team | [before](before/team.png) | [after](after/team.png) |
| me | [before](before/me.png) | [after](after/me.png) |
| team-activity | [before](before/team-activity.png) | [after](after/team-activity.png) |
| board | [before](before/board.png) | [after](after/board.png) |
| access | [before](before/access.png) | [after](after/access.png) |
| setup | [before](before/setup.png) | [after](after/setup.png) |
| review | [before](before/review.png) | [after](after/review.png) |
