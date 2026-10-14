"""Fail closed before publishing a tagged release or building its images."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tomllib
import urllib.error
import urllib.request

from scripts.write_source_manifest import build_manifest


ROOT = Path(__file__).resolve().parents[1]
SOURCE_INPUTS = [
    "pyproject.toml", "README.md", "brain", "apps", "rules", "eval", "tests",
    "scripts/write_source_manifest.py", "scripts/validate_lfm_release_gate.py",
]
DEPLOY_INPUTS = [
    "Dockerfile", "deploy/docker-compose.prod.yml", "deploy/server_up.sh",
    "scripts/write_source_manifest.py", "scripts/validate_lfm_release_gate.py",
]


def verified_tag(tag: str, revision: str, version: str) -> None:
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag):
        raise ValueError("Release tag must be vMAJOR.MINOR.PATCH")
    if tag != f"v{version}":
        raise ValueError("Release tag and package version disagree")
    tagged_revision = subprocess.check_output(
        ["git", "rev-parse", f"refs/tags/{tag}^{{commit}}"], cwd=ROOT, text=True,
    ).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", revision) or tagged_revision != revision:
        raise ValueError("Checked-out revision does not match the pushed release tag")


def verified_ci_runs(payload: dict, revision: str) -> None:
    matching = [
        run for run in payload.get("workflow_runs", [])
        if run.get("head_sha") == revision
        and run.get("event") == "push"
        and run.get("head_branch") == "main"
        and run.get("path", "").endswith("/ci.yml")
    ]
    if not matching:
        raise ValueError("No main-push CI run exists for this release revision")
    latest = max(matching, key=lambda run: run.get("id", 0))
    if latest.get("status") != "completed" or latest.get("conclusion") != "success":
        raise ValueError("The latest main-push CI run for this revision is not green")


def require_unpublished_release(repository: str, tag: str, token: str) -> None:
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/releases/tags/{tag}",
        headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20):
            raise ValueError("Release already exists for this tag; refusing asset replacement")
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise


def source_inputs() -> list[str]:
    dockerfile = (ROOT / "Dockerfile").read_text()
    copied_files = next(
        line for line in dockerfile.splitlines()
        if line.startswith("COPY pyproject.toml ")
    )
    inputs = list(SOURCE_INPUTS)
    if "requirements.lock" in copied_files:
        inputs.append("requirements.lock")
    if any(line.startswith("COPY docs/legal/THIRD_PARTY_NOTICES.md ") for line in dockerfile.splitlines()):
        inputs.append("docs/legal/THIRD_PARTY_NOTICES.md")
    return inputs


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True)
    args = parser.parse_args()
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    try:
        verified_tag(args.tag, revision, version)
    except (ValueError, subprocess.CalledProcessError) as exc:
        raise SystemExit(str(exc)) from None

    repository = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or not token:
        raise SystemExit("GitHub repository and Actions-read token are required")
    url = f"https://api.github.com/repos/{repository}/actions/workflows/ci.yml/runs?head_sha={revision}&per_page=100"
    request = urllib.request.Request(
        url, headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            verified_ci_runs(json.load(response), revision)
        require_unpublished_release(repository, args.tag, token)
    except (ValueError, TypeError) as exc:
        raise SystemExit(str(exc)) from None
    except (urllib.error.URLError, OSError) as exc:
        raise SystemExit(f"Unable to verify exact-revision CI: {type(exc).__name__}") from None

    build_time = subprocess.check_output(
        ["git", "show", "--format=%cI", "--no-patch", revision], cwd=ROOT, text=True,
    ).strip()
    source = build_manifest(ROOT, revision, includes=source_inputs())
    deploy = build_manifest(ROOT, revision, includes=DEPLOY_INPUTS)
    output = os.environ.get("GITHUB_OUTPUT")
    if not output:
        raise SystemExit("GITHUB_OUTPUT is required for the release jobs")
    with Path(output).open("a", encoding="utf-8") as handle:
        handle.write(
            f"commit_sha={revision}\nsource_digest={source['content_digest']}\n"
            f"deploy_digest={deploy['content_digest']}\nbuild_time={build_time}\n"
        )
    print(f"Verified tag {args.tag} at {revision} and exact-revision CI")


if __name__ == "__main__":
    main()
