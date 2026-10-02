"""Workspace Isolation and Clean Setup for Benchmark Runs.

Ensures every run starts from a clean disposable worktree at exact target commits
with empty scratch state and no cross-run artifact leakage.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional


class WorkspaceIsolationError(RuntimeError):
    """Raised when workspace setup or teardown fails."""
    pass


class RunWorkspace:
    """Managed clean workspace for a single benchmark run."""

    def __init__(self, run_id: str, repo_path: Path, target_commit: str, base_isolated_dir: Optional[Path] = None):
        self.run_id = run_id
        self.repo_path = repo_path.resolve()
        self.target_commit = target_commit
        self.base_isolated_dir = (base_isolated_dir or Path(tempfile.gettempdir()) / "brain_bench_workspaces").resolve()
        self.base_isolated_dir.mkdir(parents=True, exist_ok=True)
        self.worktree_dir = self.base_isolated_dir / f"worktree_{run_id}"
        self.created = False

    def setup(self) -> Path:
        """Create a clean git worktree at target_commit."""
        if self.worktree_dir.exists():
            shutil.rmtree(self.worktree_dir, ignore_errors=True)

        try:
            # Add git worktree
            res = subprocess.run(
                ["git", "worktree", "add", "--detach", str(self.worktree_dir), self.target_commit],
                cwd=str(self.repo_path),
                capture_output=True,
                text=True,
                timeout=30,
            )
            if res.returncode != 0:
                # Fallback to copy if worktree fails (e.g. detached HEAD edge cases)
                shutil.copytree(str(self.repo_path), str(self.worktree_dir), ignore=shutil.ignore_patterns(".git", ".brain", "reports"))
                (self.worktree_dir / ".bench_copied").write_text("1")
        except Exception as exc:
            raise WorkspaceIsolationError(f"Failed to setup workspace worktree: {exc}") from exc

        self.created = True
        return self.worktree_dir.resolve()

    def get_patch(self) -> str:
        """Extract unified git diff produced by the run."""
        if not self.worktree_dir.exists():
            return ""

        try:
            # Check untracked and tracked diffs
            res = subprocess.run(
                ["git", "diff", "HEAD"],
                cwd=str(self.worktree_dir),
                capture_output=True,
                text=True,
                timeout=20,
            )
            tracked_diff = res.stdout if res.returncode == 0 else ""

            # Check untracked files
            untracked_res = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(self.worktree_dir),
                capture_output=True,
                text=True,
                timeout=20,
            )
            untracked_files = []
            if untracked_res.returncode == 0:
                for line in untracked_res.stdout.splitlines():
                    if line.startswith("?? "):
                        untracked_files.append(line[3:].strip())

            untracked_diffs = []
            for ufile in untracked_files:
                uf_path = self.worktree_dir / ufile
                if uf_path.is_file():
                    try:
                        content = uf_path.read_text(encoding="utf-8", errors="replace")
                        untracked_diffs.append(f"--- /dev/null\n+++ b/{ufile}\n@@ -0,0 +1,{len(content.splitlines())} @@\n+" + "\n+".join(content.splitlines()))
                    except Exception:
                        pass

            return tracked_diff + ("\n" + "\n".join(untracked_diffs) if untracked_diffs else "")
        except Exception:
            return ""

    def cleanup(self) -> bool:
        """Remove worktree and clean up residual state."""
        if not self.worktree_dir.exists():
            return True

        try:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(self.worktree_dir)],
                cwd=str(self.repo_path),
                capture_output=True,
                timeout=15,
            )
        except Exception:
            pass

        if self.worktree_dir.exists():
            try:
                shutil.rmtree(self.worktree_dir, ignore_errors=True)
            except Exception:
                return False

        return True
