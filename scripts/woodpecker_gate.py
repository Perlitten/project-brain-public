"""Verify complete exact-revision main-push CI using the Woodpecker API."""
from __future__ import annotations

import argparse
import json
import os
import re
import urllib.parse
import urllib.request


REQUIRED_WORKFLOWS = frozenset({
    "test", "web", "lite", "lite-windows", "public-privacy",
    "container-real-docker", "team-proxy", "codeql-python", "codeql-javascript",
})


def verified_pipeline(payload: dict, revision: str) -> None:
    if (payload.get("commit") != revision or payload.get("event") != "push"
            or payload.get("branch") != "main" or payload.get("ref") != "refs/heads/main"):
        raise ValueError("Woodpecker CI is not a main-push run for the exact release revision")
    if payload.get("status") != "success":
        raise ValueError("The latest main-push Woodpecker pipeline is not green")
    workflows = {item.get("name"): item for item in payload.get("workflows", [])}
    if not REQUIRED_WORKFLOWS <= workflows.keys():
        raise ValueError("Required Woodpecker workflows are missing")
    if any(workflows[name].get("state") != "success" for name in REQUIRED_WORKFLOWS):
        raise ValueError("Required Woodpecker workflows have not all succeeded")


def verify_revision(revision: str, server: str, repo_id: str, token: str) -> dict:
    parsed = urllib.parse.urlsplit(server)
    if (parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
        raise ValueError("Woodpecker server must be an HTTPS origin")
    if not re.fullmatch(r"[1-9][0-9]*", repo_id) or not token:
        raise ValueError("Woodpecker numeric repository ID and read token are required")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("An immutable 40-character release SHA is required")

    def fetch(path):
        request = urllib.request.Request(server.rstrip("/") + "/api/" + path,
                                         headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)

    # Search all pages. Ref-filtering alone is not proof of the event or commit.
    matches: list[dict] = []
    for page in range(1, 101):
        runs = fetch(f"repos/{repo_id}/pipelines?perPage=50&page={page}")
        if not isinstance(runs, list):
            raise ValueError("Invalid Woodpecker pipeline list")
        matches.extend(run for run in runs if run.get("commit") == revision
                       and run.get("event") == "push" and run.get("ref") == "refs/heads/main")
        if len(runs) < 50:
            break
    else:
        raise ValueError("Woodpecker history exceeds the bounded search; refusing incomplete verification")
    if not matches:
        raise ValueError("No exact-revision main-push Woodpecker pipeline exists")
    latest = max(matches, key=lambda run: int(run["number"]))
    result = fetch(f"repos/{repo_id}/pipelines/{int(latest['number'])}")
    verified_pipeline(result, revision)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sha", required=True)
    args = parser.parse_args()
    try:
        result = verify_revision(args.sha, os.environ.get("BRAIN_WOODPECKER_SERVER", ""),
                                 os.environ.get("BRAIN_WOODPECKER_REPO_ID", ""),
                                 os.environ.get("BRAIN_WOODPECKER_TOKEN", ""))
    except (ValueError, OSError, KeyError, TypeError) as exc:
        raise SystemExit(f"Woodpecker release gate failed: {type(exc).__name__}: {exc}") from None
    print(f"Verified Woodpecker main-push pipeline #{result['number']} at {args.sha}")


if __name__ == "__main__":
    main()
