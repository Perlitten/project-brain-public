"""Verify public commit identity without printing private metadata.

Run on a full clone before publishing: python scripts/check_public_privacy.py.
This checks reachable commit metadata and messages, not blobs or GitHub caches.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections import Counter
from pathlib import Path
from typing import TypedDict


class PrivacyIssue(TypedDict):
    commit: str
    issue: str


PrivacyReport = TypedDict(
    "PrivacyReport", {"ref": str, "reachable_commits": int, "issues": list[PrivacyIssue], "pass": bool}
)

PUBLIC_IDENTITY = b"Perlitten <61577193+Perlitten@users.noreply.github.com>"
PERSONAL_EMAIL = re.compile(
    rb"[A-Z0-9._%+-]+@(?:gmail|yahoo|outlook|hotmail|icloud|protonmail|proton)\.[A-Z]{2,}",
    re.IGNORECASE,
)


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.PIPE)


def audit_history(repo: Path, ref: str = "HEAD", *, allow_github_identities: bool = False) -> PrivacyReport:
    if _git(repo, "rev-parse", "--is-shallow-repository").strip() == b"true":
        raise ValueError("A full clone is required; shallow history cannot pass the privacy gate")
    commits = _git(repo, "rev-list", ref).decode().splitlines()
    issues: list[PrivacyIssue] = []
    for sha in commits:
        header, message = _git(repo, "cat-file", "commit", sha).split(b"\n\n", 1)
        for role in (b"author", b"committer"):
            lines = [line for line in header.splitlines() if line.startswith(role + b" ")]
            valid = len(lines) == 1 and lines[0].rsplit(b" ", 2)[0] == role + b" " + PUBLIC_IDENTITY
            if len(lines) == 1 and allow_github_identities:
                address = re.search(rb"<([^>]+)>", lines[0])
                valid = address is not None and (
                    address[1].endswith(b"@users.noreply.github.com") or address[1] == b"noreply@github.com"
                )
            if not valid:
                issues.append({"commit": sha, "issue": f"unexpected_{role.decode()}_identity"})
        if PERSONAL_EMAIL.search(message):
            issues.append({"commit": sha, "issue": "personal_email_in_message"})
    return {"ref": ref, "reachable_commits": len(commits), "issues": issues, "pass": not issues}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--ref", default="HEAD")
    parser.add_argument(
        "--allow-github-identities", action="store_true",
        help="Allow contributor/profile names with GitHub noreply emails (for normal PR merges)",
    )
    args = parser.parse_args()
    try:
        report = audit_history(args.repo.resolve(), args.ref, allow_github_identities=args.allow_github_identities)
    except (ValueError, subprocess.CalledProcessError) as exc:
        # git errors can contain supplied ref names; do not dump captured metadata.
        print(json.dumps({"pass": False, "error": str(exc) if isinstance(exc, ValueError) else "git inspection failed"}))
        return 1
    issues = report["issues"]
    print(json.dumps({
        "ref": report["ref"],
        "reachable_commits": report["reachable_commits"],
        "pass": report["pass"],
        "affected_commits": len({issue["commit"] for issue in issues}),
        "issue_counts": dict(Counter(issue["issue"] for issue in issues)),
        "examples": issues[:10],
    }, indent=2))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
