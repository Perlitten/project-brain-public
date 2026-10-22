"""Preserve the source mutation job's same-repository PR restriction."""

import json
import os
import urllib.request


def check_payload(payload: dict, repository: str, number: str) -> None:
    head = payload.get("head") or {}
    origin = head.get("repo") or {}
    if payload.get("number") != int(number) or origin.get("full_name") != repository:
        raise SystemExit("Mutation measurement requires a same-repository pull request")


def main() -> None:
    repository = os.environ["CI_REPO"]
    number = os.environ["CI_COMMIT_PULL_REQUEST"]
    if repository != "Perlitten/project-brain-public" or not number.isascii() or not number.isdecimal():
        raise SystemExit("Invalid mutation pull-request metadata")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/pulls/{number}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "woodpecker-mutation-origin-check"},
    )
    # This repository and its PR metadata are public. Never use a broad forge token.
    # Network/rate-limit/parse failures stop the expensive measurement, without fallback.
    with urllib.request.urlopen(request, timeout=30) as response:
        check_payload(json.load(response), repository, number)
    print("Same-repository mutation PR verified")


if __name__ == "__main__":
    main()
