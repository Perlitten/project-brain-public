"""Change Laboratory facade — Phase D9.

Ties the workspace lifecycle, patch ingestion, application, supervised
validation, post-patch analysis and cleanup into one auditable session.

Project Brain may apply candidate patches only inside disposable managed
workspaces for validation. It does not apply patches to authoritative
repositories, commit changes, push branches, merge pull requests, or deploy
software.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from brain.lab.engine import (
    LabSecurityError,
    PatchApplier,
    PatchValidator,
    PostPatchAnalyzer,
    ValidationRunner,
    WorkspaceManager,
)
from brain.lab.models import (
    NETWORK_ISOLATION_STATUS,
    PatchRecord,
    ValidationProfile,
    WorkspaceState,
)

# Built-in profiles. Profiles are declarative argv arrays — never shell strings.
BUILTIN_PROFILES: Dict[str, Dict[str, Any]] = {
    "python-compile": {
        "profile_name": "python-compile",
        "timeout_seconds": 300,
        "environment": {},
        "commands": [
            {
                "argv": ["python", "-m", "compileall", "-q", "."],
                "required": True,
                "description": "Byte-compile every module in the workspace",
            }
        ],
    },
    "python-standard": {
        "profile_name": "python-standard",
        "timeout_seconds": 900,
        "environment": {},
        "commands": [
            {
                "argv": ["python", "-m", "compileall", "-q", "."],
                "required": True,
                "description": "Byte-compile every module in the workspace",
            },
            {
                "argv": ["python", "-m", "pytest", "-q"],
                "required": True,
                "description": "Run the workspace test suite",
            },
        ],
    },
}


class ProfileNotFoundError(LookupError):
    """Raised when a requested validation profile does not exist."""


class ChangeLaboratory:
    """Operator-facing entry point for validating a candidate patch."""

    def __init__(self, brain_dir: Path, ttl_seconds: int = WorkspaceManager.DEFAULT_TTL_SECONDS):
        self.brain_dir = Path(brain_dir).resolve()
        self.lab_dir = self.brain_dir / "lab"
        self.sessions_dir = self.lab_dir / "sessions"
        self.profiles_dir = self.lab_dir / "profiles"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.manager = WorkspaceManager(
            self.brain_dir / "workspaces", self.lab_dir, ttl_seconds=ttl_seconds
        )

    # ── profiles ──

    def load_profile(self, name: str) -> ValidationProfile:
        """Load a declarative profile by name: on-disk first, then built-in."""
        candidate = self.profiles_dir / f"{name}.json"
        if candidate.is_file():
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                raise ProfileNotFoundError(f"Profile '{name}' is unreadable: {exc}") from exc
            return ValidationProfile.from_dict(data)
        if name in BUILTIN_PROFILES:
            return ValidationProfile.from_dict(BUILTIN_PROFILES[name])
        raise ProfileNotFoundError(f"Unknown validation profile: '{name}'")

    def list_profiles(self) -> List[str]:
        on_disk = (
            sorted(p.stem for p in self.profiles_dir.glob("*.json"))
            if self.profiles_dir.is_dir()
            else []
        )
        return sorted(set(on_disk) | set(BUILTIN_PROFILES))

    # ── sessions ──

    def run_session(
        self,
        repository_id: str,
        repo_path: Path,
        base_revision: str,
        patch: PatchRecord,
        profile_name: str = "python-compile",
        declared_paths: Optional[Iterable[str]] = None,
        cleanup: bool = True,
        allowed_profiles: Optional[Iterable[str]] = None,
    ) -> Dict[str, Any]:
        """Validate one candidate patch end to end inside a disposable workspace."""
        session_id = f"lab-{uuid.uuid4().hex[:10]}"
        started = time.monotonic()
        session: Dict[str, Any] = {
            "session_id": session_id,
            "repository_id": repository_id,
            "base_revision": base_revision,
            "patch_id": patch.patch_id,
            "profile_name": profile_name,
            "network_isolation": NETWORK_ISOLATION_STATUS,
            "outcome": "rejected",
            "rejected_reasons": [],
            "created_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

        if allowed_profiles is not None and profile_name not in set(allowed_profiles):
            session["rejected_reasons"] = [
                f"Profile '{profile_name}' is not allowed for repository '{repository_id}'"
            ]
            return self._persist(session, started)

        try:
            profile = self.load_profile(profile_name)
        except ProfileNotFoundError as exc:
            session["rejected_reasons"] = [str(exc)]
            return self._persist(session, started)

        profile_issues = ValidationRunner.validate_profile(profile)
        if profile_issues:
            session["rejected_reasons"] = profile_issues
            return self._persist(session, started)

        # Cheap checks first: never create a workspace for an inadmissible patch.
        static_reasons = PatchValidator.validate(patch)
        if static_reasons:
            session["rejected_reasons"] = static_reasons
            return self._persist(session, started)

        record = self.manager.create_workspace(
            repository_id=repository_id, base_revision=base_revision, repo_path=Path(repo_path)
        )
        session["workspace_id"] = record.workspace_id
        session["creation_method"] = record.creation_method

        try:
            workspace_path = self.manager.workspace_path(record)
        except LabSecurityError as exc:
            self.manager.quarantine(record.workspace_id, str(exc))
            session["rejected_reasons"] = [str(exc)]
            return self._persist(session, started)

        disposable = record.workspace_id if cleanup else ""

        # Base-state checks require the tree, so they run after creation.
        stale_reasons = PatchValidator.validate(patch, workspace_path)
        if stale_reasons:
            session["rejected_reasons"] = stale_reasons
            self.manager.update_state(record.workspace_id, WorkspaceState.FAILED)
            return self._persist(session, started, cleanup_id=disposable)

        self.manager.update_state(record.workspace_id, WorkspaceState.PATCHING)
        apply_result = PatchApplier.apply(workspace_path, patch, record.workspace_id)
        session["apply"] = apply_result.to_dict()
        if not apply_result.success:
            session["outcome"] = "patch_failed"
            session["rejected_reasons"] = apply_result.rejected_reasons or [apply_result.error]
            self.manager.update_state(
                record.workspace_id,
                WorkspaceState.FAILED,
                last_error=apply_result.error[:300],
            )
            return self._persist(session, started, cleanup_id=disposable)

        analysis = PostPatchAnalyzer.analyze(
            workspace_path, apply_result, declared_paths or apply_result.affected_files
        )
        session["post_patch"] = analysis.to_dict()

        self.manager.update_state(record.workspace_id, WorkspaceState.VALIDATING)
        run_report = ValidationRunner.run_profile(
            workspace_path, profile, workspace_id=record.workspace_id
        )
        session["validation"] = run_report.to_dict()
        session["network_isolation"] = run_report.network_isolation
        session["outcome"] = "passed" if run_report.passed else "failed"
        self.manager.update_state(
            record.workspace_id,
            WorkspaceState.PASSED if run_report.passed else WorkspaceState.FAILED,
            before_hashes=apply_result.before_hashes,
            after_hashes=apply_result.after_hashes,
            validation_results=run_report.results,
            patch_files=apply_result.affected_files,
            candidate_patch_id=patch.patch_id,
        )
        return self._persist(session, started, cleanup_id=disposable)

    # ── persistence ──

    def _persist(
        self, session: Dict[str, Any], started: float, cleanup_id: str = ""
    ) -> Dict[str, Any]:
        if cleanup_id:
            session["cleanup"] = self.manager.clean_workspace(
                cleanup_id, reason="session complete"
            ).to_dict()
        session["duration_seconds"] = round(time.monotonic() - started, 3)

        path = self.sessions_dir / f"{session['session_id']}.json"
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(session, sort_keys=True, indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
        return session

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        path = self.sessions_dir / f"{session_id}.json"
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def list_sessions(self) -> List[str]:
        return sorted(p.stem for p in self.sessions_dir.glob("lab-*.json"))
