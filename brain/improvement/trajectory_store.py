"""Three-Tiered Trajectory Vault for Operational Telemetry and Replay."""

import json
from pathlib import Path
from typing import List, Optional
from brain.improvement.models import TrajectoryRecord
from brain.improvement.redaction import redact_trajectory_content
from brain.config.paths import reports_dir


class TrajectoryStore:
    """Manages raw restricted vault (14-30d), structured store (180d), and metrics store (13mo)."""

    def __init__(self, base_dir: Optional[Path] = None):
        self.base_dir = base_dir or reports_dir() / "improvement/trajectories"
        self.raw_vault = self.base_dir / "raw_vault"
        self.structured_store = self.base_dir / "structured"
        self.metrics_store = self.base_dir / "metrics"

        for p in [self.raw_vault, self.structured_store, self.metrics_store]:
            p.mkdir(parents=True, exist_ok=True)

    def save_trajectory(self, record: TrajectoryRecord) -> Path:
        """Applies 2-pass redaction and persists to structured trajectory store."""
        raw_json = json.dumps(record.model_dump(), indent=2)
        redacted_json = redact_trajectory_content(raw_json)

        target_file = self.structured_store / f"{record.trajectory_id}.json"
        target_file.write_text(redacted_json, encoding="utf-8")
        return target_file

    def get_trajectory(self, trajectory_id: str) -> Optional[TrajectoryRecord]:
        """Retrieves and parses a structured trajectory record."""
        target_file = self.structured_store / f"{trajectory_id}.json"
        if not target_file.exists():
            return None
        try:
            raw = json.loads(target_file.read_text(encoding="utf-8"))
            return TrajectoryRecord(**raw)
        except Exception:
            return None

    def list_trajectories(self, limit: int = 50) -> List[TrajectoryRecord]:
        """Lists available trajectories in the structured store."""
        records = []
        for p in sorted(self.structured_store.glob("*.json"), reverse=True)[:limit]:
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                records.append(TrajectoryRecord(**raw))
            except Exception:
                continue
        return records
