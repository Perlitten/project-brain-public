# Archive

The documents in this folder are **historical**. They are kept for provenance
only and **do not describe the current product**. Do not use them as
installation, operation or design guidance.

What is here:

- **Server-rendered dashboard specs** (`v3-*.md`, `ui/component-contracts.md`,
  `operator-ux-roadmap-2026-08-02.md`): design and porting notes for the
  Jinja dashboard that used to be served by the API under `/dashboard`. That
  dashboard was removed; the API now only redirects old `/dashboard` links to
  the separate Next.js web UI in [`apps/web/`](../../apps/web/).
- **n8n orchestration** (`phase-9-n8n-orchestration.md`): n8n was retired and
  replaced by the scheduler built into the worker — see
  [ADR 013](../adr/013-worker-scheduler.md).
- **Internal pre-release reports** (`launch-readiness/`,
  `release/acceptance-evidence.md` and its `release/screenshots/`, `demo.md`,
  `reviews/`): working notes from before the repository became public. Pull
  request and issue numbers in them (`#NN`) refer to the earlier private
  repository and do not resolve on GitHub.

For current documentation start at the top-level [README](../../README.md)
and the [0.9.0 release docs](../release/0.9.0/INSTALL.md).
