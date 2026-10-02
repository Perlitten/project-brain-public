"""Git Fixture Generator for Architectural Change Guard (Phase 10).

Creates real Git repositories with actual commits for diff-aware testing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def run_git_cmd(repo_dir: Path, args: list[str]) -> str:
    res = subprocess.run(["git"] + args, cwd=str(repo_dir), capture_output=True, text=True, check=True)
    return res.stdout.strip()


def init_git_repo(repo_dir: Path) -> str:
    """Initialize a git repository and configure user identity for commits."""
    repo_dir.mkdir(parents=True, exist_ok=True)
    run_git_cmd(repo_dir, ["init"])
    run_git_cmd(repo_dir, ["config", "user.name", "Test User"])
    run_git_cmd(repo_dir, ["config", "user.email", "test@example.com"])

    # Create initial commit
    readme = repo_dir / "README.md"
    readme.write_text("# Test Repo\n", encoding="utf-8")
    run_git_cmd(repo_dir, ["add", "README.md"])
    run_git_cmd(repo_dir, ["commit", "-m", "Initial commit"])
    return run_git_cmd(repo_dir, ["rev-parse", "HEAD"])


def setup_clean_git_fixture(repo_dir: Path) -> tuple[str, str]:
    """Create a repo where base and candidate are clean."""
    base_sha = init_git_repo(repo_dir)

    # Base commit with clean code
    f1 = repo_dir / "clean_module.py"
    f1.write_text("def helper(): return 42\n", encoding="utf-8")
    run_git_cmd(repo_dir, ["add", "clean_module.py"])
    run_git_cmd(repo_dir, ["commit", "-m", "Add clean module"])
    base_sha = run_git_cmd(repo_dir, ["rev-parse", "HEAD"])

    # Candidate commit with another clean file
    f2 = repo_dir / "another_clean.py"
    f2.write_text("def another(): return 100\n", encoding="utf-8")
    run_git_cmd(repo_dir, ["add", "another_clean.py"])
    run_git_cmd(repo_dir, ["commit", "-m", "Add another clean module"])
    cand_sha = run_git_cmd(repo_dir, ["rev-parse", "HEAD"])

    return base_sha, cand_sha


def setup_critical_violation_git_fixture(repo_dir: Path) -> tuple[str, str]:
    """Create a repo where candidate introduces a DRIFT-002 critical violation."""
    base_sha, _ = setup_clean_git_fixture(repo_dir)

    # Candidate introduces DRIFT-002 violation in worker component
    worker_dir = repo_dir / "brain" / "workers"
    worker_dir.mkdir(parents=True, exist_ok=True)
    bad_file = worker_dir / "bad_worker.py"
    bad_file.write_text(
        "import apps.api.static\n\n"
        "def process_job():\n"
        "    return apps.api.static.render()\n",
        encoding="utf-8",
    )
    run_git_cmd(repo_dir, ["add", "."])
    run_git_cmd(repo_dir, ["commit", "-m", "Introduce DRIFT-002 critical violation in worker"])
    cand_sha = run_git_cmd(repo_dir, ["rev-parse", "HEAD"])

    return base_sha, cand_sha
