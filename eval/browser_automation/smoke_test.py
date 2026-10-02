#!/usr/bin/env python3
"""Minimal Playwright smoke test — eval scaffold only, not a production Brain feature.

Pattern for a future GPT/Cursor browser bridge:
  - Separate Chromium via Playwright (not Cursor Simple Browser)
  - Read BROWSER_* env vars for headless mode and target URL
  - Return structured page facts (title, url) for agent consumption

Run:
  pip install -e ".[dev]" && playwright install chromium
  python eval/browser_automation/smoke_test.py
"""
from __future__ import annotations

import os
import sys


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def main() -> int:
    url = os.environ.get("BROWSER_SMOKE_URL", "https://example.com")
    headless = _env_bool("BROWSER_HEADLESS", True)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "playwright is not installed. Run: pip install -e \".[dev]\" && playwright install chromium",
            file=sys.stderr,
        )
        return 1

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        try:
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            title = page.title()
            final_url = page.url
        finally:
            browser.close()

    print(f"ok title={title!r} url={final_url!r} headless={headless}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
