"""The release gate must find metadata regressions in actual Git objects."""

import os
import subprocess

from scripts.check_public_privacy import audit_history


def _commit(
    repo, *, author_email="61577193+Perlitten@users.noreply.github.com",
    committer_email="61577193+Perlitten@users.noreply.github.com", message="Public change",
):
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Perlitten",
        "GIT_COMMITTER_NAME": "Perlitten",
        "GIT_AUTHOR_EMAIL": author_email,
        "GIT_COMMITTER_EMAIL": committer_email,
    }
    subprocess.run(
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", message],
        check=True,
        capture_output=True,
        env=env,
    )


def test_public_identity_passes(tmp_path):
    _commit(tmp_path)
    assert audit_history(tmp_path)["pass"] is True


def test_normal_github_merge_is_allowed_only_in_ci_mode(tmp_path):
    _commit(tmp_path, committer_email="noreply@github.com")
    assert audit_history(tmp_path)["pass"] is False
    assert audit_history(tmp_path, allow_github_identities=True)["pass"] is True


def test_private_author_fails_without_echoing_address(tmp_path):
    address = "synthetic-person@example.org"
    _commit(tmp_path, author_email=address)
    report = audit_history(tmp_path)
    assert report["pass"] is False
    assert report["issues"][0]["issue"] == "unexpected_author_identity"
    assert address not in str(report)
    assert audit_history(tmp_path, allow_github_identities=True)["pass"] is False


def test_personal_email_footer_fails_without_echoing_address(tmp_path):
    address = "synthetic-person@gmail.com"
    _commit(tmp_path, message=f"Public change\n\nCo-authored-by: Synthetic <{address}>")
    report = audit_history(tmp_path)
    assert report["pass"] is False
    assert report["issues"][0]["issue"] == "personal_email_in_message"
    assert address not in str(report)


"""Tracked-content scan: banned strings must not come back.

The launch scrub removed the owner's infrastructure and project names from the
public tree. This test keeps it that way — any reintroduction fails the gate.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# name -> pattern (case-insensitive where it is a project/person name)
BANNED_PATTERNS: dict[str, str] = {
    "private_repo_brain": r"Perlitten/Brain\b",
    "old_repo_name": r"project-brain-public",
    "owner_host_paths": r"/home/(hatch|ubuntu)\b",
    "owner_project_nostia": r"nostia",
    "owner_project_mycelium": r"MYCELIUM",
    "owner_project_forex": r"forex-bot",
    "owner_project_bpmn": r"bpmn-builder",
    "owner_watchdog": r"brain_watchdog",
    "owner_lfm_sidecars": r"deploy/lfm-colbert",
    "owner_dashboard_doc": r"OWNER_DASHBOARD",
}

# Files allowed to carry a banned string by design (never an owner leak).
ALLOWLIST: dict[str, tuple[str, ...]] = {
    # The gate's own definitions and this test's pattern list.
    "owner_project_nostia": ("tests/test_public_privacy.py",),
    "owner_project_mycelium": ("tests/test_public_privacy.py",),
    "owner_project_forex": ("tests/test_public_privacy.py",),
    "owner_project_bpmn": ("tests/test_public_privacy.py",),
    "private_repo_brain": ("tests/test_public_privacy.py",),
    "old_repo_name": ("tests/test_public_privacy.py", ".github/workflows/ci.yml", "THIRD_PARTY_NOTICES.md", "CONTRIBUTING.md", "SECURITY.md", "CODE_OF_CONDUCT.md", "README.md"),
    "owner_host_paths": ("tests/test_public_privacy.py", "eval/harness_benchmark/tasks/memory_004_many_learnings.json"),
    "owner_watchdog": ("tests/test_public_privacy.py",),
    "owner_lfm_sidecars": ("tests/test_public_privacy.py",),
    "owner_dashboard_doc": ("tests/test_public_privacy.py",),
}

_COMPILED = {name: re.compile(pat, re.IGNORECASE) for name, pat in BANNED_PATTERNS.items()}


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files"],
        check=True, capture_output=True, text=True,
    )
    return out.stdout.split()


def _scan_file(path: str):
    try:
        text = (REPO_ROOT / path).read_text(encoding="utf-8", errors="strict")
    except (UnicodeDecodeError, OSError):
        return  # binary or unreadable — nothing text-shaped to leak
    for lineno, line in enumerate(text.splitlines(), start=1):
        for name, rx in _COMPILED.items():
            if path in ALLOWLIST.get(name, ()):
                continue
            if rx.search(line):
                yield f"{path}:{lineno}: {name}"


def test_tree_has_no_banned_patterns():
    hits = [hit for path in _tracked_files() for hit in _scan_file(path)]
    assert hits == [], "banned pattern found:\n" + "\n".join(hits[:20])


def test_scanner_catches_banned_text(tmp_path):
    sample = REPO_ROOT / "tests" / "_banned_probe.tmp"
    sample.write_text("see /home/ubuntu/repos for details", encoding="utf-8")
    try:
        hits = list(_scan_file(sample.relative_to(REPO_ROOT).as_posix()))
        assert hits and "owner_host_paths" in hits[0]
    finally:
        sample.unlink(missing_ok=True)
