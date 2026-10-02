# Browser automation (optional eval scaffold)

This folder is **not** part of the production Project Brain stack. It provides a minimal
[Playwright](https://playwright.dev/python/) scaffold for local browser smoke tests and as a
reference pattern if you later wire GPT/Cursor-style browser bridges or E2E checks.

## Setup

From the `project-brain` root with your virtualenv active:

```bash
pip install -e ".[dev]"
playwright install chromium
```

On Windows PowerShell, the same commands apply after activating `.venv`.

Optional environment variables (see `.env.example`):

| Variable | Default | Purpose |
|----------|---------|---------|
| `BROWSER_HEADLESS` | `true` | Run Chromium headless (`false` to watch the window) |
| `BROWSER_SMOKE_URL` | `https://example.com` | URL opened by the smoke test |

## Smoke test

```bash
python eval/browser_automation/smoke_test.py
```

The script launches Chromium, navigates to the target URL, prints the page title, and exits.
Use it to verify Playwright is installed before building custom E2E flows.

## Future bridge pattern (not implemented)

A typical agent/browser bridge would:

1. Launch or attach to a **separate** Playwright browser (not Cursor's embedded Simple Browser).
2. Expose steps (`goto`, `click`, `fill`, `snapshot`) over stdio/MCP or a small HTTP shim.
3. Return structured page state (title, URL, accessibility tree or screenshot) to the agent.

Project Brain does **not** ship such a bridge today. See [`docs/tooling-gaps.md`](../../docs/tooling-gaps.md)
for platform limits (Computer Use, Cursor embedded browser, Puppeteer vs Playwright).
