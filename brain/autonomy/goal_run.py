"""Disabled compatibility surface for the unfinished GoalRun executor.

Workflow:
SUBMIT -> validate scope/permissions -> freeze contract -> queue -> plan -> context -> isolated worktree -> implement -> test -> diagnose/fix -> full regression -> commit/push -> deploy -> verify -> close.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from brain.autonomy.models import (
    GoalPhase,
    ExecutionCheckpoint,
)
from brain.autonomy.notifications import emit_goal_notification
from brain.autonomy.state import SessionStore
from brain.lab.engine import WorkspaceManager

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class GoalRun:
    """Represents a single production engineering GoalRun with full provenance & state."""

    def __init__(
        self,
        goal_id: str,
        description: str,
        target_file: Optional[str] = None,
        oracle_test: Optional[str] = None,
        requires_deploy: bool = False,
    ):
        self.goal_id = goal_id
        self.description = description
        self.target_file = target_file
        self.oracle_test = oracle_test
        self.requires_deploy = requires_deploy
        self.current_phase = GoalPhase.GOAL_RECEIVED
        self.status = "QUEUED"
        self.worktree_path: Optional[str] = None
        self.commit_sha: Optional[str] = None
        self.deployed: bool = False
        self.checkpoints: List[ExecutionCheckpoint] = []
        self.telemetry: List[Any] = []
        self.fencing_token: str = str(uuid.uuid4())
        self.lease_expires_at: float = time.time() + 300.0
        self.created_at_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class GoalRunService:
    """Production GoalRun Service managing goal queues, worktrees, and deployments."""

    def __init__(self, session_store: Optional[SessionStore] = None):
        self.store = session_store or SessionStore()
        ws_dir = PROJECT_ROOT / "context_packs" / "worktrees"
        storage_dir = PROJECT_ROOT / "context_packs" / "storage"
        self.workspace_mgr = WorkspaceManager(workspaces_dir=ws_dir, storage_dir=storage_dir)
        self.active_runs: Dict[str, GoalRun] = {}

    def _validate_safety(self, goal_id: str, target_file: Optional[str], description: str) -> Tuple[bool, str]:
        """Validate safety policies: block path traversal, secret reading, root command execution."""
        target_str = str(target_file or "")
        desc_str = str(description or "")

        if ".." in target_str or target_str.startswith("/") or target_str.startswith("\\"):
            return False, "Path traversal or absolute path outside repository denied"

        if ".env" in target_str or "secret" in target_str.lower() or "shadow" in target_str.lower():
            return False, "Access to environment credentials or secrets denied"

        if "root_exec" in target_str or "sudo" in desc_str or "root" in desc_str:
            return False, "Unrestricted root or arbitrary shell execution denied"

        return True, "Scope valid"

    async def submit_goal(
        self,
        goal_id: str,
        description: str,
        target_file: Optional[str] = None,
        oracle_test: Optional[str] = None,
        requires_deploy: bool = False,
    ) -> Dict[str, Any]:
        """Asynchronously validate and enqueue a goal request."""
        return {"status": "BLOCKED", "phase": "BLOCKED_ESCALATED", "goal_id": goal_id,
                "detail": "Autonomous execution is unavailable: no verified implementation/commit/deploy executor exists."}

    async def execute_goal_run(self, goal_id: str) -> GoalRun:
        """Process queued goal in worker thread with fencing lease and full worktree verification."""
        run = self.active_runs.get(goal_id)
        if run is None:
            run = GoalRun(goal_id=goal_id, description=f"Goal {goal_id}")
        if run.status != "CANCELLED":
            run.status = "BLOCKED"
            run.current_phase = GoalPhase.BLOCKED_ESCALATED
        return run

    def get_goal_status(self, goal_id: str) -> Optional[GoalRun]:
        return self.active_runs.get(goal_id)

    def cancel_goal(self, goal_id: str) -> bool:
        """Race-safe goal cancellation."""
        run = self.active_runs.get(goal_id)
        if not run:
            return False
        run.status = "CANCELLED"
        run.current_phase = GoalPhase.BLOCKED_ESCALATED
        emit_goal_notification(goal_id, "CANCELLED", "User requested cancellation")
        return True

    def _run_subprocess_test(self, oracle_test: str) -> Tuple[bool, str]:
        if oracle_test.startswith("-"):
            return False, "Rejected oracle_test: must be a test path, not a pytest option."
        try:
            completed = subprocess.run(
                [Path(sys.executable).as_posix(), "-m", "pytest", "-v", "--tb=short", "--", oracle_test],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            return completed.returncode == 0, completed.stdout
        except subprocess.TimeoutExpired as exc:
            stdout = (
                exc.stdout.decode(errors="replace")
                if isinstance(exc.stdout, bytes)
                else exc.stdout or ""
            )
            stderr = (
                exc.stderr.decode(errors="replace")
                if isinstance(exc.stderr, bytes)
                else exc.stderr or ""
            )
            return False, f"Test execution timed out after {exc.timeout}s:\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"
        except Exception as exc:
            return False, str(exc)
