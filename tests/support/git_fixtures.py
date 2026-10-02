"""Real Git fixture helpers.

Workstreams C–G are validated against genuine Git repositories with genuine
commits — mocking Git would hide exactly the failures these workstreams exist
to catch. Every helper here creates repositories under pytest's ``tmp_path``,
so nothing is ever written inside the authoritative checkout.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Dict, Iterable, Optional

_ENV_IDENTITY = [
    "-c",
    "user.name=Brain Fixture",
    "-c",
    "user.email=fixture@brain.local",
    "-c",
    "commit.gpgsign=false",
]


def git(repo: Path, *args: str, check: bool = True) -> str:
    """Run a Git command inside ``repo`` and return stdout."""
    proc = subprocess.run(
        ["git", *_ENV_IDENTITY, *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {repo}: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.stdout


def init_repo(path: Path, default_branch: str = "main") -> Path:
    """Initialise an empty Git repository with a deterministic identity."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", default_branch],
        cwd=str(path),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    git(path, "config", "user.name", "Brain Fixture")
    git(path, "config", "user.email", "fixture@brain.local")
    git(path, "config", "commit.gpgsign", "false")
    # Independent of the host's global core.autocrlf: checkouts and worktrees
    # must keep LF so patch text matches the working tree byte for byte.
    git(path, "config", "core.autocrlf", "false")
    git(path, "config", "core.eol", "lf")
    return path


def write_files(repo: Path, files: Dict[str, str]) -> None:
    for rel, content in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        # Newlines stay LF so hand-written unified diffs apply on Windows too.
        target.write_text(content, encoding="utf-8", newline="\n")


def commit_all(repo: Path, message: str) -> str:
    """Stage everything and commit; returns the new commit SHA."""
    git(repo, "add", "-A")
    git(repo, "commit", "-m", message, "--allow-empty")
    return head(repo)


def commit_files(repo: Path, files: Dict[str, str], message: str) -> str:
    write_files(repo, files)
    return commit_all(repo, message)


def delete_files(repo: Path, paths: Iterable[str]) -> None:
    for rel in paths:
        target = repo / rel
        if target.exists():
            target.unlink()


def rename_file(repo: Path, old: str, new: str) -> None:
    git(repo, "mv", old, new)


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD").strip()


def checkout(repo: Path, revision: str) -> None:
    git(repo, "checkout", "--quiet", revision)


def make_python_repo(
    root: Path,
    name: str = "fixture-repo",
    extra_files: Optional[Dict[str, str]] = None,
) -> Path:
    """Create a small but real Python repository with two commits of history."""
    repo = init_repo(root / name)
    files = {
        # Local Brain runtime state must never enter the fixture's history:
        # otherwise `git checkout` of an older revision deletes the very graph
        # generations under test.
        ".gitignore": ".brain/\n__pycache__/\n",
        "README.md": f"# {name}\n",
        "pkg/__init__.py": "",
        "pkg/core.py": (
            "import json\n\n\n"
            "class Core:\n"
            "    def run(self):\n"
            "        return json.dumps({'ok': True})\n"
        ),
        "pkg/util.py": "def helper(value):\n    return value * 2\n",
    }
    if extra_files:
        files.update(extra_files)
    commit_files(repo, files, "init: fixture repository")
    return repo
