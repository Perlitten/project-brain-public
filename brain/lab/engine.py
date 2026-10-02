"""Change Laboratory core — Phases D2, D3, D4, D5, D6, D7, D8.

Disposable managed workspaces, patch ingestion and application, supervised
validation execution, post-patch analysis, and cleanup with quarantine.

Boundaries enforced here, not merely documented:

* patches are applied only inside a directory proven to be a managed workspace;
* nothing is deleted unless the path resolves inside the managed workspace root
  *and* carries the ownership marker of the expected workspace record;
* no command is ever built from a shell string and ``shell=True`` is never used;
* executable names come from a fixed allowlist, never from repository content;
* the child environment is built from an allowlist, so process secrets are not
  inherited by validation commands;
* timeouts terminate the whole process group, not just the direct child;
* validation commands run inside an OS sandbox when one is available
  (``brain/lab/sandbox.py``: disposable container or unprivileged namespaces);
  the report records the mechanism actually used — ``enforced:<backend>`` or
  ``unverified``, never a claim that wasn't exercised.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from brain.lab.models import (
    WORKSPACE_MARKER,
    CleanupReport,
    PatchApplyResult,
    PatchRecord,
    PostPatchReport,
    ValidationProfile,
    ValidationResult,
    ValidationRunReport,
    WorkspaceRecord,
    WorkspaceState,
)
from brain.lab.sandbox import (
    network_isolation_status,
    resolve_sandbox_backend,
    wrap_argv,
)


class LabSecurityError(RuntimeError):
    """Raised when an operation would leave the managed workspace boundary."""


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ──────────────────────────────────────────────
# Containment
# ──────────────────────────────────────────────


class WorkspaceGuard:
    """Containment checks for every path the laboratory touches."""

    @staticmethod
    def resolve_inside(root: Path, candidate: Path) -> Path:
        """Resolve ``candidate`` and prove it stays inside ``root``.

        Symlinks are resolved *before* the containment comparison, so a symlink
        planted inside a workspace cannot be used to reach outside it.
        """
        root_resolved = root.resolve()
        try:
            resolved = candidate.resolve()
        except OSError as exc:  # pragma: no cover - platform dependent
            raise LabSecurityError(f"Cannot resolve path '{candidate}': {exc}") from exc
        if resolved != root_resolved and root_resolved not in resolved.parents:
            raise LabSecurityError(
                f"Path '{resolved}' is outside the managed workspace root '{root_resolved}'"
            )
        return resolved

    @staticmethod
    def is_inside(root: Path, candidate: Path) -> bool:
        try:
            WorkspaceGuard.resolve_inside(root, candidate)
            return True
        except LabSecurityError:
            return False

    @staticmethod
    def read_marker(workspace_path: Path) -> Optional[Dict[str, Any]]:
        marker = workspace_path / WORKSPACE_MARKER
        if not marker.is_file():
            return None
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def verify_ownership(record: WorkspaceRecord, managed_root: Path) -> Path:
        """Return the workspace path only if it is contained *and* owned."""
        if not record.workspace_root:
            raise LabSecurityError(f"Workspace '{record.workspace_id}' has no recorded root")
        path = WorkspaceGuard.resolve_inside(managed_root, Path(record.workspace_root))
        if not path.is_dir():
            raise LabSecurityError(f"Workspace directory does not exist: {path}")
        marker = WorkspaceGuard.read_marker(path)
        if marker is None:
            raise LabSecurityError(f"Workspace directory is not marked as managed: {path}")
        if marker.get("workspace_id") != record.workspace_id:
            raise LabSecurityError(
                f"Workspace marker at '{path}' belongs to "
                f"'{marker.get('workspace_id')}', not '{record.workspace_id}'"
            )
        return path


# ──────────────────────────────────────────────
# Phase D3: patch ingestion
# ──────────────────────────────────────────────


class PatchValidator:
    """Validates candidate patches before they are allowed near a workspace."""

    _DIFF_GIT = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+)$")
    _HEADER = re.compile(r"^(?:---|\+\+\+) (?:[ab]/)?(?P<path>.+?)\s*$")
    _SYMLINK_MODE = re.compile(r"^(?:new|old|new file|deleted file) mode 120000$")

    @staticmethod
    def target_paths(patch_content: str) -> List[str]:
        """Every repository-relative path the patch claims to touch."""
        paths: List[str] = []
        for line in patch_content.splitlines():
            match = PatchValidator._DIFF_GIT.match(line)
            if match:
                paths.extend([match.group("a"), match.group("b")])
                continue
            if line.startswith("--- ") or line.startswith("+++ "):
                header = PatchValidator._HEADER.match(line)
                if header:
                    candidate = header.group("path")
                    if candidate not in {"/dev/null"}:
                        paths.append(candidate)
        # Preserve first-seen order without duplicates.
        seen: Dict[str, None] = {}
        for path in paths:
            seen.setdefault(path.strip(), None)
        return [p for p in seen if p]

    @staticmethod
    def validate(patch: PatchRecord, workspace_path: Optional[Path] = None) -> List[str]:
        """Return rejection reasons; an empty list means the patch is admissible."""
        reasons: List[str] = []

        if not patch.patch_content.strip():
            reasons.append("Patch content is empty")
            return reasons

        encoded = patch.patch_content.encode("utf-8", errors="replace")
        if len(encoded) > patch.max_bytes:
            reasons.append(f"Patch exceeds max bytes ({len(encoded)} > {patch.max_bytes})")

        if "\x00" in patch.patch_content:
            reasons.append("Patch contains NUL bytes")

        for line in patch.patch_content.splitlines():
            if PatchValidator._SYMLINK_MODE.match(line.strip()):
                reasons.append("Patch creates or modifies a symlink (mode 120000)")
                break

        targets = PatchValidator.target_paths(patch.patch_content)
        if not targets:
            reasons.append("Patch declares no target files")

        distinct = {t for t in targets}
        if len(distinct) > patch.max_files:
            reasons.append(f"Patch modifies too many files ({len(distinct)} > {patch.max_files})")

        allowed = [a.replace("\\", "/").strip("/") for a in patch.allowed_paths]
        for target in targets:
            normalized = target.replace("\\", "/")
            if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
                reasons.append(f"Patch targets an absolute path: {target}")
                continue
            parts = normalized.split("/")
            if ".." in parts:
                reasons.append(f"Path traversal detected: {target}")
                continue
            if parts and parts[0] == ".git":
                reasons.append(f"Patch targets the .git directory: {target}")
                continue
            if normalized == WORKSPACE_MARKER:
                reasons.append("Patch targets the workspace ownership marker")
                continue
            if allowed and not any(
                normalized == a or normalized.startswith(f"{a}/") for a in allowed
            ):
                reasons.append(f"Path outside declared scope: {target}")

        if workspace_path is not None and patch.expected_file_hashes:
            reasons.extend(PatchValidator._verify_base_state(patch, workspace_path))

        return reasons

    @staticmethod
    def _verify_base_state(patch: PatchRecord, workspace_path: Path) -> List[str]:
        """Reject a patch whose recorded base state no longer matches the tree."""
        reasons: List[str] = []
        for rel_path, expected in patch.expected_file_hashes.items():
            try:
                target = WorkspaceGuard.resolve_inside(workspace_path, workspace_path / rel_path)
            except LabSecurityError as exc:
                reasons.append(str(exc))
                continue
            if not target.is_file():
                reasons.append(f"Patch base file is missing: {rel_path}")
                continue
            actual = _sha256_file(target)
            if actual != expected:
                reasons.append(
                    f"Patch is stale: '{rel_path}' hash {actual[:12]} != expected {expected[:12]}"
                )
        return reasons


# ──────────────────────────────────────────────
# Phase D2 / D8: workspace lifecycle
# ──────────────────────────────────────────────


# `shutil.ignore_patterns` returns a plain function; kept at module level so it
# is never bound as a method and handed the wrong argument count by copytree.
_COPY_IGNORE = shutil.ignore_patterns(
    ".git", ".brain", "__pycache__", ".venv", "venv", "node_modules", ".mypy_cache"
)


class WorkspaceManager:
    """Creates, tracks, and disposes of disposable validation workspaces."""

    DEFAULT_TTL_SECONDS = 3600

    def __init__(
        self,
        workspaces_dir: Path,
        storage_dir: Path,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ):
        self._ws_dir = Path(workspaces_dir).resolve()
        self._ws_dir.mkdir(parents=True, exist_ok=True)
        self._storage_dir = Path(storage_dir).resolve()
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        self._records_file = self._storage_dir / "workspaces.json"
        self._ttl_seconds = max(60, int(ttl_seconds))
        self._lock = threading.RLock()

    # ── properties ──

    @property
    def managed_root(self) -> Path:
        return self._ws_dir

    @property
    def storage_dir(self) -> Path:
        return self._storage_dir

    # ── creation ──

    def create_workspace(
        self,
        repository_id: str,
        base_revision: str,
        repo_path: Path,
        capabilities: Optional[Iterable[str]] = None,
        ttl_seconds: Optional[int] = None,
    ) -> WorkspaceRecord:
        """Create a disposable workspace at ``base_revision``.

        A Git worktree is preferred; a filtered copy is the fallback for
        repositories that are not Git checkouts or where worktrees are refused.
        """
        source = Path(repo_path).resolve()
        if not source.is_dir():
            raise ValueError(f"Source repository does not exist: {source}")

        ws_id = f"ws-{uuid.uuid4().hex[:10]}"
        ws_dir = self._ws_dir / ws_id
        if ws_dir.exists():  # pragma: no cover - uuid collision
            raise ValueError(f"Workspace directory already exists: {ws_dir}")

        method = "copy"
        worktree_error = ""
        try:
            result = subprocess.run(
                ["git", "worktree", "add", "--detach", str(ws_dir), base_revision],
                cwd=str(source),
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode == 0:
                method = "git_worktree"
            else:
                worktree_error = (result.stderr or result.stdout).strip()[:300]
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            worktree_error = str(exc)[:300]

        if method == "copy":
            if ws_dir.exists():
                shutil.rmtree(ws_dir, ignore_errors=True)
            shutil.copytree(str(source), str(ws_dir), ignore=_COPY_IGNORE, symlinks=False)

        ttl = max(60, int(ttl_seconds or self._ttl_seconds))
        record = WorkspaceRecord(
            workspace_id=ws_id,
            repository_id=repository_id,
            base_revision=base_revision,
            workspace_root=str(ws_dir.resolve()),
            source_repository_root=str(source),
            creation_method=method,
            state=WorkspaceState.READY.value,
            capabilities=sorted(capabilities) if capabilities else [],
            created_at_utc=_utc_now(),
            updated_at_utc=_utc_now(),
            expires_at_utc=time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + ttl)
            ),
            process_owner=f"pid:{os.getpid()}",
            last_error=worktree_error if method == "copy" else "",
        )
        self._write_marker(ws_dir, record)
        self._save_record(record)
        return record

    def _write_marker(self, ws_dir: Path, record: WorkspaceRecord) -> None:
        marker = {
            "workspace_id": record.workspace_id,
            "repository_id": record.repository_id,
            "base_revision": record.base_revision,
            "created_at_utc": record.created_at_utc,
            "managed_by": "project-brain-change-laboratory",
        }
        (ws_dir / WORKSPACE_MARKER).write_text(
            json.dumps(marker, sort_keys=True, indent=2), encoding="utf-8"
        )

    # ── reads ──

    def get_workspace(self, workspace_id: str) -> Optional[WorkspaceRecord]:
        raw = self._load_records().get(workspace_id)
        return WorkspaceRecord.from_dict(raw) if raw else None

    def list_workspaces(self) -> List[WorkspaceRecord]:
        return [WorkspaceRecord.from_dict(v) for v in self._load_records().values()]

    def workspace_path(self, record: WorkspaceRecord) -> Path:
        """Contained, ownership-verified path of a workspace."""
        return WorkspaceGuard.verify_ownership(record, self._ws_dir)

    # ── state ──

    def update_state(
        self,
        workspace_id: str,
        state: WorkspaceState,
        **fields: Any,
    ) -> Optional[WorkspaceRecord]:
        with self._lock:
            records = self._load_records()
            if workspace_id not in records:
                return None
            entry = records[workspace_id]
            entry["state"] = state.value
            entry["updated_at_utc"] = _utc_now()
            entry["version"] = int(entry.get("version", 1)) + 1
            for key, value in fields.items():
                if key in WorkspaceRecord.__dataclass_fields__:
                    entry[key] = value
            self._save_records(records)
            return WorkspaceRecord.from_dict(entry)

    def expire_due(self, now: Optional[float] = None) -> List[str]:
        """Mark workspaces whose TTL has passed. Returns the affected ids."""
        moment = time.gmtime(now if now is not None else time.time())
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", moment)
        expired: List[str] = []
        for record in self.list_workspaces():
            if record.state in {
                WorkspaceState.CLEANED.value,
                WorkspaceState.QUARANTINED.value,
                WorkspaceState.EXPIRED.value,
            }:
                continue
            if record.expires_at_utc and record.expires_at_utc <= stamp:
                self.update_state(record.workspace_id, WorkspaceState.EXPIRED)
                expired.append(record.workspace_id)
        return expired

    # ── disposal ──

    def clean_workspace(self, workspace_id: str, reason: str = "") -> CleanupReport:
        """Dispose of a workspace, or quarantine it if disposal is not provably safe."""
        report = CleanupReport(workspace_id=workspace_id, reason=reason)
        record = self.get_workspace(workspace_id)
        if record is None:
            report.reason = f"Unknown workspace '{workspace_id}'"
            return report

        recorded_path = Path(record.workspace_root) if record.workspace_root else None
        if recorded_path is None or not recorded_path.exists():
            # Nothing to delete: the directory is already gone.
            self.update_state(workspace_id, WorkspaceState.CLEANED, cleanup_status="absent")
            report.removed = True
            report.reason = reason or "workspace directory already absent"
            return report

        try:
            path = WorkspaceGuard.verify_ownership(record, self._ws_dir)
        except LabSecurityError as exc:
            # Refuse to delete anything we cannot prove we own.
            self.update_state(
                workspace_id,
                WorkspaceState.QUARANTINED,
                cleanup_status="refused",
                quarantine_reason=str(exc),
            )
            report.quarantined = True
            report.reason = str(exc)
            return report

        self.update_state(workspace_id, WorkspaceState.CLEANING)

        if record.creation_method == "git_worktree" and record.source_repository_root:
            try:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(path)],
                    cwd=record.source_repository_root,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                pass

        if path.exists():
            try:
                shutil.rmtree(path, onerror=_force_remove)
            except OSError as exc:
                self.update_state(
                    workspace_id,
                    WorkspaceState.QUARANTINED,
                    cleanup_status="failed",
                    quarantine_reason=str(exc)[:300],
                )
                report.quarantined = True
                report.reason = str(exc)[:300]
                return report

        self.update_state(workspace_id, WorkspaceState.CLEANED, cleanup_status="removed")
        report.removed = True
        report.reason = reason or "cleaned"
        return report

    def quarantine(self, workspace_id: str, reason: str) -> Optional[WorkspaceRecord]:
        return self.update_state(
            workspace_id,
            WorkspaceState.QUARANTINED,
            quarantine_reason=reason[:300],
            cleanup_status="quarantined",
        )

    def recover_orphans(self) -> CleanupReport:
        """Reconcile the managed root with the record store.

        Directories with no record are removed only when they carry a valid
        ownership marker; anything else is reported and left untouched.
        """
        report = CleanupReport(workspace_id="", reason="orphan sweep")
        known = {r.workspace_id: r for r in self.list_workspaces()}
        live_dirs = {p.name: p for p in self._ws_dir.iterdir() if p.is_dir()}

        for name, path in sorted(live_dirs.items()):
            if name in known and known[name].state not in {
                WorkspaceState.CLEANED.value,
            }:
                continue
            marker = WorkspaceGuard.read_marker(path)
            report.orphan_directories.append(str(path))
            if marker is None or marker.get("workspace_id") != name:
                continue
            try:
                shutil.rmtree(WorkspaceGuard.resolve_inside(self._ws_dir, path), onerror=_force_remove)
            except (OSError, LabSecurityError):
                continue

        for workspace_id, record in sorted(known.items()):
            if record.state in {WorkspaceState.CLEANED.value, WorkspaceState.QUARANTINED.value}:
                continue
            if workspace_id not in live_dirs:
                report.missing_workspaces.append(workspace_id)
                self.update_state(
                    workspace_id, WorkspaceState.CLEANED, cleanup_status="vanished"
                )
        return report

    # ── persistence ──

    def _save_record(self, record: WorkspaceRecord) -> None:
        with self._lock:
            records = self._load_records()
            records[record.workspace_id] = record.to_dict()
            self._save_records(records)

    def _load_records(self) -> Dict[str, Any]:
        if not self._records_file.exists():
            return {}
        try:
            data = json.loads(self._records_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_records(self, data: Dict[str, Any]) -> None:
        tmp = self._records_file.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, sort_keys=True, indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(self._records_file)


def _force_remove(func, path, _exc_info):  # pragma: no cover - platform dependent
    """rmtree error handler: clear the read-only bit Git leaves on Windows."""
    try:
        os.chmod(path, 0o700)
        func(path)
    except OSError:
        raise


# ──────────────────────────────────────────────
# Phase D4: patch application
# ──────────────────────────────────────────────


class PatchApplier:
    """Applies patches inside disposable workspaces, and only there."""

    MAX_SNAPSHOT_BYTES = 20 * 1024 * 1024

    @staticmethod
    def apply(
        workspace_path: Path,
        patch: PatchRecord,
        workspace_id: str = "",
    ) -> PatchApplyResult:
        """Apply ``patch`` inside ``workspace_path`` with rollback on failure."""
        result = PatchApplyResult(workspace_id=workspace_id, patch_id=patch.patch_id)
        root = workspace_path.resolve()

        targets = [
            t.replace("\\", "/")
            for t in PatchValidator.target_paths(patch.patch_content)
        ]
        try:
            resolved_targets = [
                WorkspaceGuard.resolve_inside(root, root / t) for t in targets
            ]
        except LabSecurityError as exc:
            result.rejected_reasons.append(str(exc))
            result.error = str(exc)
            return result

        snapshot = PatchApplier._snapshot(root, resolved_targets)
        result.before_hashes = {
            PatchApplier._rel(root, p): _sha256_file(p)
            for p in resolved_targets
            if p.is_file()
        }

        # The patch file lives outside the workspace so it can never be picked
        # up by validation commands or by post-patch analysis.
        handle, patch_file = tempfile.mkstemp(prefix="brain-patch-", suffix=".diff")
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="") as fh:
                fh.write(patch.patch_content)

            check = PatchApplier._git_apply(root, patch_file, check_only=True)
            if check.returncode != 0:
                result.error = (check.stderr or check.stdout).strip()[:500]
                result.rejected_reasons.append("git apply --check rejected the patch")
                return result

            applied = PatchApplier._git_apply(root, patch_file, check_only=False)
            if applied.returncode != 0:
                result.error = (applied.stderr or applied.stdout).strip()[:500]
                result.rolled_back = PatchApplier._rollback(root, snapshot)
                return result
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            result.error = str(exc)[:500]
            result.rolled_back = PatchApplier._rollback(root, snapshot)
            return result
        finally:
            try:
                os.unlink(patch_file)
            except OSError:  # pragma: no cover
                pass

        result.success = True
        result.affected_files = sorted(
            {PatchApplier._rel(root, p) for p in resolved_targets}
        )
        result.after_hashes = {
            PatchApplier._rel(root, p): _sha256_file(p)
            for p in resolved_targets
            if p.is_file()
        }
        return result

    @staticmethod
    def _git_apply(root: Path, patch_file: str, check_only: bool) -> subprocess.CompletedProcess:
        argv = ["git", "apply", "--whitespace=nowarn"]
        if check_only:
            argv.append("--check")
        argv.append(patch_file)
        return subprocess.run(
            argv, cwd=str(root), capture_output=True, text=True, timeout=120
        )

    @staticmethod
    def _snapshot(root: Path, targets: List[Path]) -> Dict[str, Optional[bytes]]:
        """Byte-exact snapshot of the files a patch claims to touch."""
        snapshot: Dict[str, Optional[bytes]] = {}
        budget = PatchApplier.MAX_SNAPSHOT_BYTES
        for path in targets:
            key = PatchApplier._rel(root, path)
            if not path.is_file():
                snapshot[key] = None  # did not exist before
                continue
            size = path.stat().st_size
            if size > budget:
                continue
            budget -= size
            snapshot[key] = path.read_bytes()
        return snapshot

    @staticmethod
    def _rollback(root: Path, snapshot: Dict[str, Optional[bytes]]) -> bool:
        """Restore the snapshot; returns True when the tree was fully restored."""
        restored = True
        for rel, content in snapshot.items():
            target = root / rel
            try:
                WorkspaceGuard.resolve_inside(root, target)
            except LabSecurityError:
                restored = False
                continue
            try:
                if content is None:
                    if target.exists():
                        target.unlink()
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
            except OSError:
                restored = False
        return restored

    @staticmethod
    def _rel(root: Path, path: Path) -> str:
        try:
            return path.relative_to(root).as_posix()
        except ValueError:  # pragma: no cover - guarded by containment
            return path.name


# ──────────────────────────────────────────────
# Phases D5 / D6: validation profiles and supervised execution
# ──────────────────────────────────────────────


class ValidationRunner:
    """Runs declarative validation profiles under process supervision."""

    ALLOWED_EXECUTABLES = {"python", "python3", "py", "pytest", "ruff", "mypy"}
    MAX_COMMANDS = 20
    MAX_TIMEOUT_SECONDS = 3600
    MAX_OUTPUT_CHARS = 5000

    # Only these variables reach a validation process. Everything else — API
    # keys, tokens, database URLs — stays in the parent process.
    ENV_ALLOWLIST = (
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "PYTHONIOENCODING",
        "PYTHONHASHSEED",
        "PYTHONDONTWRITEBYTECODE",
    )
    _SECRETISH = ("SECRET", "TOKEN", "PASSWORD", "API_KEY", "PRIVATE_KEY", "CREDENTIAL")
    _SECRET_PATTERNS = re.compile(
        r"(API_KEY|PASSWORD|SECRET|TOKEN|PRIVATE_KEY)([\s=:]+)\S+", re.IGNORECASE
    )

    # ── profile validation ──

    @staticmethod
    def validate_profile(profile: ValidationProfile) -> List[str]:
        issues: List[str] = []
        if profile.schema_version != 1:
            issues.append(f"Unsupported profile schema version: {profile.schema_version}")
        if profile.timeout_seconds <= 0:
            issues.append("Profile timeout must be positive")
        if profile.timeout_seconds > ValidationRunner.MAX_TIMEOUT_SECONDS:
            issues.append(
                f"Timeout too large: {profile.timeout_seconds}s "
                f"(max {ValidationRunner.MAX_TIMEOUT_SECONDS})"
            )
        if not profile.commands:
            issues.append("Profile declares no commands")
        if len(profile.commands) > ValidationRunner.MAX_COMMANDS:
            issues.append(
                f"Too many commands: {len(profile.commands)} (max {ValidationRunner.MAX_COMMANDS})"
            )

        for key in profile.environment:
            if any(marker in key.upper() for marker in ValidationRunner._SECRETISH):
                issues.append(f"Profile environment declares a secret-like variable: {key}")

        for idx, cmd in enumerate(profile.commands):
            if not cmd.argv:
                issues.append(f"Command {idx}: empty argv")
                continue
            if not all(isinstance(arg, str) for arg in cmd.argv):
                issues.append(f"Command {idx}: argv must be a list of strings")
                continue
            executable = cmd.argv[0]
            # Repository-provided executable paths are never trusted: only bare
            # names from the allowlist are accepted.
            if "/" in executable or "\\" in executable or Path(executable).is_absolute():
                issues.append(f"Command {idx}: executable path not allowed: '{executable}'")
                continue
            if executable.lower().removesuffix(".exe") not in ValidationRunner.ALLOWED_EXECUTABLES:
                issues.append(f"Command {idx}: executable '{executable}' not in allowlist")
        return issues

    # ── execution ──

    @staticmethod
    def build_environment(profile_environment: Dict[str, str]) -> Dict[str, str]:
        env = {
            key: os.environ[key]
            for key in ValidationRunner.ENV_ALLOWLIST
            if key in os.environ
        }
        for key, value in profile_environment.items():
            if any(marker in key.upper() for marker in ValidationRunner._SECRETISH):
                continue
            env[key] = str(value)
        # The sandbox backend enforces this when one is available (see
        # brain/lab/sandbox.py); the report records the actual mechanism.
        env.setdefault("BRAIN_LAB_NETWORK_POLICY", "deny")
        return env

    @staticmethod
    def run_profile(
        workspace_path: Path,
        profile: ValidationProfile,
        cancel_event: Optional[threading.Event] = None,
        workspace_id: str = "",
    ) -> ValidationRunReport:
        started = time.monotonic()
        report = ValidationRunReport(
            workspace_id=workspace_id,
            profile_name=profile.profile_name,
        )

        issues = ValidationRunner.validate_profile(profile)
        if issues:
            report.rejected_reasons = issues
            report.duration_seconds = round(time.monotonic() - started, 3)
            return report

        root = workspace_path.resolve()
        if not root.is_dir():
            report.rejected_reasons = [f"Workspace directory does not exist: {root}"]
            report.duration_seconds = round(time.monotonic() - started, 3)
            return report

        env = ValidationRunner.build_environment(profile.environment)
        backend = resolve_sandbox_backend()
        report.network_isolation = network_isolation_status(backend)
        deadline = time.monotonic() + profile.timeout_seconds
        results: List[ValidationResult] = []
        passed = True

        for idx, cmd in enumerate(profile.commands):
            if cancel_event is not None and cancel_event.is_set():
                report.cancelled = True
                break

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                results.append(
                    ValidationResult(
                        command_idx=idx,
                        argv=list(cmd.argv),
                        exit_code=-1,
                        timed_out=True,
                        required=cmd.required,
                        stderr_snippet="Profile timeout exhausted before this command started",
                    )
                )
                passed = False
                break

            result = ValidationRunner._run_one(
                root, cmd.argv, env, remaining, idx, cmd.required, cancel_event, backend
            )
            results.append(result)
            # Cancellation is checked before the exit code: a killed command
            # exits non-zero, and reporting that as a plain validation failure
            # would let an operator read "the patch failed" into "I stopped it".
            if cancel_event is not None and cancel_event.is_set():
                report.cancelled = True
                break
            if result.exit_code != 0 and cmd.required:
                passed = False
                break

        report.results = [r.to_dict() for r in results]
        report.passed = bool(results) and passed and not report.cancelled
        report.duration_seconds = round(time.monotonic() - started, 3)
        return report

    @staticmethod
    def _run_one(
        root: Path,
        argv: List[str],
        env: Dict[str, str],
        timeout: float,
        idx: int,
        required: bool,
        cancel_event: Optional[threading.Event],
        backend: str = "off",
    ) -> ValidationResult:
        started = time.monotonic()
        result = ValidationResult(command_idx=idx, argv=list(argv), required=required)
        resolved = ValidationRunner._resolve_executable(argv[0])
        if resolved is None:
            result.exit_code = -1
            result.stderr_snippet = f"Executable not found on PATH: {argv[0]}"
            result.duration_seconds = round(time.monotonic() - started, 3)
            return result

        popen_kwargs: Dict[str, Any] = {
            "cwd": str(root),
            "env": env,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
        }
        # Own process group, so a timeout kills the whole tree rather than
        # leaving orphaned grandchildren behind.
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True

        popen_argv, sandbox = wrap_argv(
            backend, workspace=root, argv=[resolved, *argv[1:]], env=env, resolved_exe=Path(resolved)
        )

        try:
            process = subprocess.Popen(popen_argv, **popen_kwargs)
        except (FileNotFoundError, OSError) as exc:
            result.exit_code = -1
            result.stderr_snippet = str(exc)[:500]
            result.duration_seconds = round(time.monotonic() - started, 3)
            return result

        stdout, stderr = "", ""
        try:
            if cancel_event is None:
                stdout, stderr = process.communicate(timeout=timeout)
            else:
                stdout, stderr = ValidationRunner._communicate_cancellable(
                    process, timeout, cancel_event
                )
                if cancel_event.is_set() and process.returncode is None:
                    raise _Cancelled()
        except subprocess.TimeoutExpired:
            sandbox.terminate()
            ValidationRunner._terminate_group(process)
            stdout, stderr = ValidationRunner._drain(process)
            result.timed_out = True
            result.exit_code = -1
            stderr = (stderr or "") + f"\nCommand exceeded {int(timeout)}s and was terminated"
        except _Cancelled:
            sandbox.terminate()
            ValidationRunner._terminate_group(process)
            stdout, stderr = ValidationRunner._drain(process)
            result.exit_code = -1
            stderr = (stderr or "") + "\nCommand cancelled by operator"
        else:
            result.exit_code = process.returncode

        result.stdout_snippet = ValidationRunner._redact(
            (stdout or "")[: ValidationRunner.MAX_OUTPUT_CHARS]
        )
        result.stderr_snippet = ValidationRunner._redact(
            (stderr or "")[: ValidationRunner.MAX_OUTPUT_CHARS]
        )
        result.duration_seconds = round(time.monotonic() - started, 3)
        return result

    @staticmethod
    def _communicate_cancellable(
        process: subprocess.Popen, timeout: float, cancel_event: threading.Event
    ) -> Tuple[str, str]:
        deadline = time.monotonic() + timeout
        while True:
            slice_timeout = min(0.25, max(0.01, deadline - time.monotonic()))
            try:
                return process.communicate(timeout=slice_timeout)
            except subprocess.TimeoutExpired:
                if cancel_event.is_set():
                    return "", ""
                if time.monotonic() >= deadline:
                    raise

    @staticmethod
    def _terminate_group(process: subprocess.Popen) -> None:
        """Terminate the child *and* its descendants."""
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                    capture_output=True,
                    timeout=30,
                )
            else:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
            pass
        finally:
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:  # pragma: no cover
                    pass

    @staticmethod
    def _drain(process: subprocess.Popen) -> Tuple[str, str]:
        try:
            return process.communicate(timeout=10)
        except (subprocess.TimeoutExpired, ValueError, OSError):  # pragma: no cover
            return "", ""

    @staticmethod
    def _resolve_executable(name: str) -> Optional[str]:
        """Resolve an allowlisted name against PATH, never against the workspace."""
        base = name.lower().removesuffix(".exe")
        if base not in ValidationRunner.ALLOWED_EXECUTABLES:
            return None
        if base in {"python", "python3", "py"}:
            return sys.executable
        return shutil.which(name)

    @staticmethod
    def _redact(text: str) -> str:
        return ValidationRunner._SECRET_PATTERNS.sub(r"\1\2<REDACTED>", text)


class _Cancelled(Exception):
    """Internal signal: the operator cancelled a running validation."""


# ──────────────────────────────────────────────
# Phase D7: post-patch analysis
# ──────────────────────────────────────────────


class PostPatchAnalyzer:
    """Reports what the patch actually changed inside the workspace."""

    @staticmethod
    def analyze(
        workspace_path: Path,
        apply_result: PatchApplyResult,
        declared_paths: Optional[Iterable[str]] = None,
    ) -> PostPatchReport:
        root = workspace_path.resolve()
        declared = sorted({p.replace("\\", "/") for p in (declared_paths or [])})
        report = PostPatchReport(
            workspace_id=apply_result.workspace_id, declared_files=declared
        )

        git_status = PostPatchAnalyzer._git_status(root)
        if git_status is not None:
            report.analysis_method = "git_status"
            report.added_files, report.changed_files, report.deleted_files = git_status
        else:
            report.analysis_method = "hash_comparison"
            for path, before in apply_result.before_hashes.items():
                after = apply_result.after_hashes.get(path)
                if after is None:
                    report.deleted_files.append(path)
                elif after != before:
                    report.changed_files.append(path)
            for path in apply_result.after_hashes:
                if path not in apply_result.before_hashes:
                    report.added_files.append(path)

        touched = sorted(
            set(report.added_files) | set(report.changed_files) | set(report.deleted_files)
        )
        report.added_files.sort()
        report.changed_files.sort()
        report.deleted_files.sort()

        if declared:
            report.unexpected_files = [
                p
                for p in touched
                if not any(p == d or p.startswith(f"{d}/") for d in declared)
            ]
            report.within_declared_scope = not report.unexpected_files
        return report

    @staticmethod
    def _git_status(root: Path) -> Optional[Tuple[List[str], List[str], List[str]]]:
        # Only when *this* directory is itself a checkout. A copied workspace
        # nested inside some other repository must not report that repository's
        # status as if it were the patch's effect.
        if not (root / ".git").exists():
            return None
        try:
            proc = subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=all"],
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return None
        if proc.returncode != 0:
            return None

        added: List[str] = []
        changed: List[str] = []
        deleted: List[str] = []
        for line in proc.stdout.splitlines():
            if len(line) < 4:
                continue
            code, path = line[:2], line[3:].strip().strip('"')
            if path == WORKSPACE_MARKER:
                continue
            if "D" in code:
                deleted.append(path)
            elif code.strip() in {"??", "A"}:
                added.append(path)
            else:
                changed.append(path)
        return added, changed, deleted
