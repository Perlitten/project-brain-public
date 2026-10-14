"""Autonomous Self-Healing Engineering Operator Engine (v0.9.0).

Executes bounded repository goals end-to-end with zero human supervision:
GOAL -> internal plan -> verified context -> execute -> test -> diagnose -> repair -> re-test -> commit/push -> deploy -> verify -> close.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from brain.autonomy.models import (
    GoalSession,
    GoalPhase,
    ExecutionCheckpoint,
    EscalationRecord,
    EscalationReason,
)
from brain.autonomy.state import SessionStore
from brain.operations.incidents import IncidentStateMachine
from brain.operations.remediation import AutonomicRemediationEngine

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class AutonomousOperatorEngine:
    """Engine executing autonomous closed-loop goal resolution with zero human supervision."""

    def __init__(self, session_store: Optional[SessionStore] = None):
        self.store = session_store or SessionStore()
        self.incident_sm = IncidentStateMachine()
        self.remediation_engine = AutonomicRemediationEngine()

    def start_session(self, session_id: str, goal_description: str) -> GoalSession:
        existing = self.store.load(session_id)
        if existing:
            return existing

        session = GoalSession(
            session_id=session_id,
            goal_description=goal_description,
            current_phase=GoalPhase.GOAL_RECEIVED,
        )
        self.store.save(session)
        return session

    async def execute_goal(self, session_id: str, oracle_test: Optional[str] = None) -> GoalSession:
        session = self.store.load(session_id)
        if not session:
            raise ValueError(f"Session {session_id} not found.")

        if session.is_closed or session.is_blocked:
            return session

        # No implementation/provider/commit/deploy executor is wired. Do not
        # manufacture test, completion or billing evidence from this scaffold.
        session.is_blocked = True
        session.current_phase = GoalPhase.BLOCKED_ESCALATED
        self.store.save(session)
        return session

    def escalate_boundary(
        self,
        session_id: str,
        reason: EscalationReason,
        description: str,
        provenance_proof: str,
    ) -> GoalSession:
        session = self.store.load(session_id)
        if not session:
            raise ValueError(f"Session {session_id} not found.")

        rec = EscalationRecord(
            escalation_id=f"esc_{len(session.escalations)+1}",
            reason=reason,
            description=description,
            provenance_proof=provenance_proof,
        )
        session.escalations.append(rec)
        session.current_phase = GoalPhase.BLOCKED_ESCALATED
        session.is_blocked = True
        self._add_checkpoint(session, GoalPhase.BLOCKED_ESCALATED)
        self.store.save(session)
        return session

    def _add_checkpoint(
        self,
        session: GoalSession,
        phase: GoalPhase,
        passed_tests: Optional[List[str]] = None,
        failed_tests: Optional[List[str]] = None,
        patch_id: Optional[str] = None,
    ):
        cp = ExecutionCheckpoint(
            checkpoint_id=f"chk_{len(session.checkpoints)+1}",
            phase=phase,
            attempt_count=len(session.checkpoints) + 1,
            passed_tests=passed_tests or [],
            failed_tests=failed_tests or [],
            patch_id=patch_id,
        )
        session.checkpoints.append(cp)

    def _run_test(self, oracle_test: Optional[str]) -> Tuple[bool, str]:
        if not oracle_test:
            return True, "No oracle test provided."
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
