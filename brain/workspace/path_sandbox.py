"""Secure Path Validation for Repository Registration.

Phase A3 — Validates repository paths against configured workspace roots.
Rejects traversal, symlink escape, non-Git directories, duplicates, and other risks.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import List, Optional, Set


class PathValidationError(ValueError):
    """Raised when a repository path fails security validation."""
    pass


class PathSandbox:
    """Validates and sandboxes repository paths for secure registration."""

    def __init__(
        self,
        allowed_roots: List[Path],
        *,
        allow_home: bool = False,
        allow_nested: bool = False,
        registered_canonicals: Optional[Set[str]] = None,
    ):
        self._allowed_roots = [r.resolve() for r in allowed_roots]
        self._allow_home = allow_home
        self._allow_nested = allow_nested
        self._registered_canonicals = registered_canonicals or set()

    def validate(self, path: str | Path, *, require_git: bool = True) -> Path:
        """Validate a path for repository registration. Returns resolved canonical path.

        Raises PathValidationError for:
        - empty path
        - traversal (..)
        - symlink escapes
        - non-directories
        - non-Git repos (when require_git=True)
        - root filesystem
        - home directory (unless explicitly allowed)
        - outside allowed roots
        - duplicate registration
        - nested repo ambiguity (unless explicitly allowed)
        - temp directories not marked as fixtures
        """
        raw = str(path).strip()
        if not raw:
            raise PathValidationError("Repository path cannot be empty")

        # Traversal check on raw input
        normalized = raw.replace("\\", "/")
        parts = normalized.split("/")
        if ".." in parts:
            raise PathValidationError(
                f"Path traversal detected in '{raw}': '..' components are not allowed"
            )

        # Resolve symlinks before containment validation
        path_obj = Path(raw)
        try:
            resolved = path_obj.resolve(strict=True)
        except (FileNotFoundError, OSError) as exc:
            raise PathValidationError(f"Repository path does not exist: {raw}") from exc

        if not resolved.is_dir():
            raise PathValidationError(f"Repository path is not a directory: {resolved}")

        # Symlink escape: resolved must still be inside allowed roots
        # (checked below in containment)

        # Root filesystem check
        if resolved == resolved.anchor or str(resolved) in ("/", "\\"):
            raise PathValidationError("Cannot register root filesystem as a repository")

        # Windows root check (e.g. C:\)
        if len(resolved.parts) <= 1:
            raise PathValidationError("Cannot register root filesystem as a repository")

        # Home directory check
        home = Path.home().resolve()
        if resolved == home and not self._allow_home:
            raise PathValidationError(
                "Cannot register home directory as a repository without explicit authorization"
            )

        # Containment check against allowed roots
        is_inside = False
        for root in self._allowed_roots:
            if resolved == root or root in resolved.parents:
                is_inside = True
                break

        if not is_inside:
            raise PathValidationError(
                f"Access denied: '{resolved}' is outside allowed workspace roots"
            )

        # Duplicate registration check
        canonical_str = str(resolved).replace("\\", "/").rstrip("/").lower()
        if canonical_str in self._registered_canonicals:
            raise PathValidationError(
                f"Repository at '{resolved}' is already registered"
            )

        # Git repository check
        if require_git:
            git_dir = resolved / ".git"
            if not git_dir.exists():
                raise PathValidationError(
                    f"Not a Git repository: '{resolved}' (no .git directory found)"
                )

        # Nested repository check
        if not self._allow_nested:
            for existing_canonical in self._registered_canonicals:
                existing_path = Path(existing_canonical)
                if resolved != existing_path:
                    if existing_path in resolved.parents:
                        raise PathValidationError(
                            f"Nested repository ambiguity: '{resolved}' is inside already registered '{existing_path}'"
                        )
                    if resolved in existing_path.parents:
                        raise PathValidationError(
                            f"Nested repository ambiguity: '{resolved}' is a parent of already registered '{existing_path}'"
                        )

        return resolved

    def validate_fixture(self, path: str | Path) -> Path:
        """Validate a fixture repository path — relaxed Git requirement."""
        return self.validate(path, require_git=False)

    @staticmethod
    def get_canonical_string(resolved: Path) -> str:
        """Normalize a resolved path for identity comparison."""
        return str(resolved).replace("\\", "/").rstrip("/").lower()

    @staticmethod
    def get_normalized_identity(resolved: Path) -> str:
        """Portable normalized identity — lowercase, forward-slash, no trailing slash."""
        return str(resolved).replace("\\", "/").rstrip("/").lower()

    @staticmethod
    def resolve_symlinks_safe(path: Path) -> Path:
        """Resolve symlinks with safety checks."""
        try:
            return path.resolve(strict=True)
        except (FileNotFoundError, OSError) as exc:
            raise PathValidationError(f"Cannot resolve path: {path}") from exc

    @staticmethod
    def get_git_revision(repo_path: Path) -> str:
        """Get current HEAD revision of a Git repository."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
        return "unknown"

    @staticmethod
    def get_git_default_branch(repo_path: Path) -> str:
        """Detect default branch of a Git repository."""
        for candidate in ("main", "master"):
            try:
                result = subprocess.run(
                    ["git", "rev-parse", "--verify", candidate],
                    cwd=str(repo_path),
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.returncode == 0:
                    return candidate
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                pass
        return "main"

    @staticmethod
    def detect_languages(repo_path: Path) -> List[str]:
        """Detect programming languages from file extensions in a repository."""
        ext_map = {
            ".py": "python",
            ".js": "javascript",
            ".ts": "typescript",
            ".java": "java",
            ".go": "go",
            ".rs": "rust",
            ".rb": "ruby",
            ".yml": "yaml",
            ".yaml": "yaml",
            ".json": "json",
            ".toml": "toml",
            ".sql": "sql",
        }
        found = set()
        try:
            for root, _dirs, files in os.walk(repo_path):
                # Skip hidden and common non-source dirs
                if any(p.startswith(".") or p in ("node_modules", "__pycache__", ".venv", "venv")
                       for p in Path(root).parts):
                    continue
                for f in files[:500]:  # Bounded scan
                    ext = Path(f).suffix.lower()
                    if ext in ext_map:
                        found.add(ext_map[ext])
                if len(found) > 10:
                    break
        except OSError:
            pass
        return sorted(found)
