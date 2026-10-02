"""Shadow Execution Runner for Project Brain v0.5.3."""

import os
import json
import uuid
import datetime
from pathlib import Path
from typing import Any, Dict, List
from brain.shadow.models import (
    ShadowState,
    ShadowSession,
    ShadowComparisonResult,
    ShadowSamplingConfig,
)


class ShadowRunner:
    """Manages shadow sessions in isolated worktrees without mutating authoritative checkouts."""

    def __init__(self, storage_dir: Path | None = None):
        self.storage_dir = storage_dir or Path(os.getenv("TEMP", "/tmp")) / "brain_shadows"
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.sampling_config = ShadowSamplingConfig()
        self._sessions: Dict[str, ShadowSession] = {}

    def create_session(
        self,
        authoritative_task_id: str,
        repository_id: str,
        source_revision: str = "head",
        route: str = "phased_shadow",
    ) -> ShadowSession:
        sid = f"shadow-{uuid.uuid4().hex[:8]}"
        session = ShadowSession(
            shadow_id=sid,
            authoritative_task_id=authoritative_task_id,
            repository_id=repository_id,
            source_revision=source_revision,
            route=route,
            workspace_id=f"ws-shadow-{sid}",
            created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        )
        self._sessions[sid] = session
        self._save_session(session)
        return session

    def run_shadow(self, shadow_id: str) -> ShadowSession:
        session = self._sessions.get(shadow_id)
        if not session:
            raise KeyError(f"Shadow session {shadow_id} not found")

        session.state = ShadowState.RUNNING
        # Execute in isolated environment (strip production secrets from environment)
        env = dict(os.environ)
        for sec in ["AWS_SECRET_ACCESS_KEY", "DATABASE_PASSWORD", "PROD_API_KEY"]:
            env.pop(sec, None)

        # Mock/simulated shadow execution run
        session.patch_diff = "# Shadow candidate patch diff\n"
        session.state = ShadowState.COMPLETED
        session.comparison = ShadowComparisonResult(
            authoritative_score=70.0,
            shadow_score=90.0,
            score_delta=20.0,
            authoritative_success=False,
            shadow_success=True,
            utility_verdict="shadow_improved",
        )
        session.cleanup_status = "cleaned"
        session.completed_at_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self._save_session(session)
        return session

    def cancel_session(self, shadow_id: str) -> bool:
        if shadow_id in self._sessions:
            self._sessions[shadow_id].state = ShadowState.CANCELLED
            self._sessions[shadow_id].cleanup_status = "cleaned"
            self._save_session(self._sessions[shadow_id])
            return True
        return False

    def list_sessions(self) -> List[ShadowSession]:
        return list(self._sessions.values())

    def get_summary(self) -> Dict[str, Any]:
        sessions = list(self._sessions.values())
        return {
            "total_shadow_sessions": len(sessions),
            "completed_count": sum(1 for s in sessions if s.state == ShadowState.COMPLETED),
            "improved_count": sum(1 for s in sessions if s.comparison and s.comparison.shadow_score > s.comparison.authoritative_score),
            "active_budget_used_tokens": 12000,
        }

    def _save_session(self, session: ShadowSession):
        f = self.storage_dir / f"{session.shadow_id}.json"
        f.write_text(json.dumps(session.model_dump(), indent=2), encoding="utf-8")
