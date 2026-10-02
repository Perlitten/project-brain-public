"""Durable Session State & Checkpoint Store for Autonomous Engineering Operator."""
import json
import time
from pathlib import Path
from typing import Optional, List
from brain.autonomy.models import GoalSession

SESSIONS_DIR = Path(__file__).resolve().parent.parent.parent / "context_packs" / "sessions"


class SessionStore:
    """Manages durable GoalSession state on disk for idempotent, crash-resumable execution."""

    def __init__(self, storage_dir: Optional[Path] = None):
        self.storage_dir = storage_dir or SESSIONS_DIR
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    def _get_path(self, session_id: str) -> Path:
        return self.storage_dir / f"session_{session_id}.json"

    def save(self, session: GoalSession) -> None:
        session.updated_at_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        file_path = self._get_path(session.session_id)
        temp_path = file_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(session.model_dump(), indent=2), encoding="utf-8")
        temp_path.replace(file_path)

    def load(self, session_id: str) -> Optional[GoalSession]:
        file_path = self._get_path(session_id)
        if not file_path.is_file():
            return None
        try:
            raw = json.loads(file_path.read_text(encoding="utf-8"))
            return GoalSession(**raw)
        except Exception:
            corrupted_path = file_path.with_suffix(".json.corrupted")
            try:
                file_path.replace(corrupted_path)
            except Exception:
                pass
            return None

    def list_sessions(self) -> List[GoalSession]:
        sessions = []
        for file_path in self.storage_dir.glob("session_*.json"):
            try:
                raw = json.loads(file_path.read_text(encoding="utf-8"))
                sessions.append(GoalSession(**raw))
            except Exception:
                corrupted_path = file_path.with_suffix(".json.corrupted")
                try:
                    file_path.replace(corrupted_path)
                except Exception:
                    pass
                continue
        return sessions
