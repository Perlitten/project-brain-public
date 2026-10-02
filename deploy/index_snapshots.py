#!/usr/bin/env python3
"""Index mounted project snapshots through the local Project Brain API."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def load_env_value(env_path: Path, key: str) -> str:
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name == key:
            return value.strip().strip('"').strip("'")
    raise RuntimeError(f"{key} is missing in {env_path}")


def post_index(api_url: str, api_key: str, repo_path: str, timeout: int) -> tuple[int, str]:
    payload = json.dumps({"repo_path": repo_path}).encode("utf-8")
    request = urllib.request.Request(
        f"{api_url.rstrip('/')}/index",
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-API-Key": api_key,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("repos", nargs="+", help="Mounted repo paths, e.g. /indexed/Eunoia")
    parser.add_argument("--api-url", default="http://127.0.0.1:8010")
    parser.add_argument("--env", default=str(Path.home() / "project-brain" / ".env"))
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()

    api_key = load_env_value(Path(args.env), "PROJECT_BRAIN_API_KEY")
    failed = False

    for repo_path in args.repos:
        started = time.monotonic()
        print(f"::INDEX::{repo_path}", flush=True)
        status, body = post_index(args.api_url, api_key, repo_path, args.timeout)
        elapsed = time.monotonic() - started
        print(f"status={status} elapsed={elapsed:.1f}s")
        print(body)
        if status >= 400:
            failed = True

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
