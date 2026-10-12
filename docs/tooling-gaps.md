# Tooling gaps

This document lists browser and UI automation capabilities that **cannot** be added inside
the Project Brain repository, and what **was** added as an optional local scaffold.

---

## What is missing (platform / agent limits)

### Computer Use / desktop screen clicker

Driving arbitrary clicks on the desktop (a screen clicker, OS-level Computer Use) is only
possible when **the IDE or the operating system** exposes such an API to the agent. It is not
part of Project Brain and cannot be configured from this repository.

### Direct control of Cursor's embedded browser

There is no public API for controlling **Cursor's Simple Browser** or any other IDE-embedded
browser tab. Agents in chat cannot "attach" to that window from code in this repository.

**Workarounds:**

- a separate browser driven by **Playwright** (see `eval/browser_automation/`);
- manually copying URLs or screenshots between the agent and the user;
- external APIs with their own browser tool, if your client supports them.

### Puppeteer / Selenium

- **Puppeteer** belongs to the Node.js ecosystem; the primary path in this repository is
  **Python Playwright**. Puppeteer can be used from a separate Node project if needed.
- **Selenium** is intentionally not included: Playwright covers the typical E2E scenarios
  with fewer dependencies.

| Capability | Status in repo |
|------------|----------------|
| Computer Use / desktop screen clicker | **Not available** — requires IDE/OS agent APIs |
| Control Cursor embedded Simple Browser | **No API** — use Playwright, copy-paste, or external browser tools |
| Puppeteer | **Not bundled** — Node.js; Playwright (Python) is the primary path |
| Selenium | **Not bundled** — Playwright is sufficient for planned E2E |

---

## What was added

- **`playwright`** in the `pyproject.toml` dev dependencies (`pip install -e ".[dev]"`).
- **`eval/browser_automation/`** — README and smoke test (`smoke_test.py`), configured through the
  optional `BROWSER_HEADLESS` and `BROWSER_SMOKE_URL` environment variables.
- **README § Browser automation (optional)** — how to install and run the smoke test.

This scaffold is for **optional local** browser automation and future E2E experiments. It does
**not** grant agents control over Cursor's UI or the host desktop.
