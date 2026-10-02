"""Git Change-Set Engine for Architectural Change Guard (Phase 2).

Safely executes Git CLI subprocess commands without shell injection risks,
resolving changed files, renames, and modified line ranges.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Set

from brain.config.paths import validate_secure_repo_path


@dataclass
class GitFileChange:
    """Represents a single file change between Git revisions."""

    old_path: Optional[str]
    new_path: str
    change_type: str  # 'added', 'modified', 'deleted', 'renamed'
    base_sha: Optional[str] = None
    candidate_sha: Optional[str] = None
    language: str = "python"
    is_ignored: bool = False
    changed_lines: List[int] = field(default_factory=list)


class GitDiffEngine:
    """Safe Git subprocess execution for change-set analysis."""

    def __init__(self, repo_path: Path):
        self.repo_path = validate_secure_repo_path(repo_path)

    def _run_git(self, args: List[str]) -> str:
        try:
            res = subprocess.run(
                ["git"] + args,
                cwd=str(self.repo_path),
                capture_output=True,
                text=True,
                check=True,
            )
            return res.stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            stderr = exc.stderr if hasattr(exc, "stderr") else str(exc)
            raise ValueError(f"Git command failed: git {' '.join(args)}: {stderr}")

    def resolve_revision(self, rev: str) -> str:
        """Validate and resolve Git revision to full 40-char commit SHA."""
        if not rev or not isinstance(rev, str):
            raise ValueError("Revision string cannot be empty")
        # Sanitize revision string (only alphanumeric, ~, ^, ., _, -, /)
        if not re.match(r"^[a-zA-Z0-9_\-.~/^]+$", rev):
            raise ValueError(f"Invalid Git revision syntax: {rev}")
        return self._run_git(["rev-parse", "--verify", f"{rev}^{{commit}}"])

    def get_changed_files(self, base_rev: str = "HEAD~1", candidate_rev: str = "HEAD") -> List[str]:
        """Return list of modified/added file path strings between base and candidate."""
        try:
            changes = self.get_changeset(base_rev, candidate_rev)
            return [c.new_path for c in changes if c.change_type != "deleted"]
        except Exception:
            return []

    def get_changeset(
        self,
        base_rev: str = "HEAD~1",
        candidate_rev: str = "HEAD",
        include_untracked: bool = False,
    ) -> List[GitFileChange]:
        """Compute structured file changes between base and candidate revisions."""
        base_sha = self.resolve_revision(base_rev)
        candidate_sha = self.resolve_revision(candidate_rev) if candidate_rev != "WORKING_TREE" else "WORKING_TREE"

        diff_args = ["diff", "--name-status", "-M"]
        if candidate_sha == "WORKING_TREE":
            diff_args.append(base_sha)
        else:
            diff_args.extend([base_sha, candidate_sha])

        raw_diff = self._run_git(diff_args)
        changes: List[GitFileChange] = []
        seen_paths: Set[str] = set()

        for line in raw_diff.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            status_code = parts[0][0].upper()

            if status_code == "A":
                change_type = "added"
                new_path = parts[1]
                old_path = None
            elif status_code == "M":
                change_type = "modified"
                new_path = parts[1]
                old_path = new_path
            elif status_code == "D":
                change_type = "deleted"
                new_path = parts[1]
                old_path = new_path
            elif status_code == "R":
                change_type = "renamed"
                old_path = parts[1]
                new_path = parts[2]
            else:
                change_type = "modified"
                new_path = parts[-1]
                old_path = new_path

            # Normalize relative POSIX path
            norm_path = new_path.replace("\\", "/").lstrip("/")
            seen_paths.add(norm_path)

            # Get changed lines for Python files
            changed_lines = []
            if norm_path.endswith(".py") and change_type != "deleted":
                changed_lines = self._get_changed_line_numbers(base_sha, candidate_sha, norm_path)

            changes.append(
                GitFileChange(
                    old_path=old_path.replace("\\", "/").lstrip("/") if old_path else None,
                    new_path=norm_path,
                    change_type=change_type,
                    base_sha=base_sha,
                    candidate_sha=candidate_sha,
                    language="python" if norm_path.endswith(".py") else "other",
                    changed_lines=changed_lines,
                )
            )

        if include_untracked:
            untracked = self._get_untracked_files()
            for u_path in untracked:
                if u_path not in seen_paths and u_path.endswith(".py"):
                    changes.append(
                        GitFileChange(
                            old_path=None,
                            new_path=u_path,
                            change_type="added",
                            base_sha=base_sha,
                            candidate_sha=candidate_sha,
                            language="python",
                        )
                    )

        return changes

    def _get_changed_line_numbers(self, base_sha: str, candidate_sha: str, rel_path: str) -> List[int]:
        """Parse unified diff line numbers added or modified in candidate."""
        try:
            diff_args = ["diff", "-U0"]
            if candidate_sha == "WORKING_TREE":
                diff_args.extend([base_sha, "--", rel_path])
            else:
                diff_args.extend([base_sha, candidate_sha, "--", rel_path])

            diff_text = self._run_git(diff_args)
            added_lines: List[int] = []
            for line in diff_text.splitlines():
                if line.startswith("@@"):
                    # Format @@ -old_line,count +new_line,count @@
                    m = re.search(r"\+([0-9]+)(?:,([0-9]+))?", line)
                    if m:
                        start_line = int(m.group(1))
                        count = int(m.group(2)) if m.group(2) else 1
                        if count == 0:
                            added_lines.append(start_line)
                        else:
                            added_lines.extend(range(start_line, start_line + count))
            return added_lines
        except Exception:
            return []

    def _get_untracked_files(self) -> List[str]:
        try:
            raw = self._run_git(["status", "--porcelain"])
            untracked = []
            for line in raw.splitlines():
                if line.startswith("??"):
                    p = line[3:].strip().replace("\\", "/").lstrip("/")
                    untracked.append(p)
            return untracked
        except Exception:
            return []
