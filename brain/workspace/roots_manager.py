"""Workspace Roots Manager.

Phase A5 — Managed root directories for repositories, worktrees,
artifacts, caches, experiments, and quarantine.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any, Dict, Optional


class WorkspaceRootsManager:
    """Manages structured workspace root directories with safe creation and cleanup."""

    # Root subdirectories
    REPOS = "repositories"
    WORKTREES = "worktrees"
    ARTIFACTS = "artifacts"
    GRAPH_STAGING = "graph-staging"
    EXPERIMENTS = "experiments"
    CACHES = "caches"
    QUARANTINE = "quarantine"
    FAILED_RETENTION = "failed-workspaces"

    ALL_ROOTS = [REPOS, WORKTREES, ARTIFACTS, GRAPH_STAGING, EXPERIMENTS, CACHES, QUARANTINE, FAILED_RETENTION]

    def __init__(self, base_dir: Path):
        self._base = base_dir.resolve()

    @property
    def base(self) -> Path:
        return self._base

    def initialize(self) -> Dict[str, Path]:
        """Create all managed root directories. Returns mapping of name → path."""
        result = {}
        for name in self.ALL_ROOTS:
            p = self._base / name
            p.mkdir(parents=True, exist_ok=True)
            result[name] = p
        return result

    def get_root(self, name: str) -> Path:
        """Get a managed root path."""
        if name not in self.ALL_ROOTS:
            raise ValueError(f"Unknown workspace root: {name}")
        p = self._base / name
        p.mkdir(parents=True, exist_ok=True)
        return p

    def validate_containment(self, path: Path, expected_root: str) -> bool:
        """Verify a path is inside the expected managed root."""
        root = self.get_root(expected_root)
        try:
            resolved = path.resolve()
            return resolved == root or root in resolved.parents
        except (OSError, ValueError):
            return False

    def create_workspace_dir(
        self,
        root_name: str,
        workspace_id: str,
    ) -> Path:
        """Create a new workspace directory under a managed root."""
        root = self.get_root(root_name)
        workspace_dir = root / workspace_id

        if workspace_dir.exists():
            raise ValueError(f"Workspace directory already exists: {workspace_dir}")

        # Safety: ensure we're not being tricked by symlinks
        workspace_dir.mkdir(parents=True, exist_ok=False)
        resolved = workspace_dir.resolve()

        if not self.validate_containment(resolved, root_name):
            # Shouldn't happen, but defensive
            try:
                workspace_dir.rmdir()
            except OSError:
                pass
            raise ValueError(
                f"Created workspace directory escapes managed root: {resolved}"
            )

        return resolved

    def safe_remove(self, path: Path, expected_root: str) -> bool:
        """Safely remove a directory verified to be inside the expected root.

        Returns True if removed, False if path didn't exist.
        Raises ValueError if path escapes the root.
        """
        if not path.exists():
            return False

        resolved = path.resolve()
        if not self.validate_containment(resolved, expected_root):
            raise ValueError(
                f"Cannot remove: '{resolved}' is outside managed root '{expected_root}'"
            )

        try:
            shutil.rmtree(resolved)
            return True
        except OSError:
            return False

    def quarantine(
        self,
        path: Path,
        source_root: str,
        reason: str,
    ) -> Optional[Path]:
        """Move a failed workspace to quarantine for inspection."""
        if not path.exists():
            return None

        resolved = path.resolve()
        if not self.validate_containment(resolved, source_root):
            raise ValueError(
                f"Cannot quarantine: '{resolved}' is outside managed root '{source_root}'"
            )

        quarantine_root = self.get_root(self.QUARANTINE)
        ts = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
        quarantine_dest = quarantine_root / f"{resolved.name}_{ts}"

        try:
            shutil.move(str(resolved), str(quarantine_dest))
            # Record reason
            meta = quarantine_dest / "_quarantine_reason.txt"
            meta.write_text(f"Reason: {reason}\nOriginal: {resolved}\nTime: {ts}\n")
            return quarantine_dest
        except OSError:
            return None

    def find_orphans(self, root_name: str, known_ids: set[str]) -> list[Path]:
        """Find directories in a root that don't match known workspace IDs."""
        root = self.get_root(root_name)
        orphans = []
        try:
            for child in root.iterdir():
                if child.is_dir() and child.name not in known_ids:
                    orphans.append(child)
        except OSError:
            pass
        return orphans

    def get_usage(self) -> Dict[str, Any]:
        """Get disk usage summary for all managed roots."""
        usage = {}
        for name in self.ALL_ROOTS:
            root = self._base / name
            if root.exists():
                total = 0
                count = 0
                try:
                    for p in root.rglob("*"):
                        if p.is_file():
                            total += p.stat().st_size
                            count += 1
                except OSError:
                    pass
                usage[name] = {"bytes": total, "files": count}
            else:
                usage[name] = {"bytes": 0, "files": 0}
        return usage
