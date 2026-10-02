"""PostgreSQL Control Plane for Trajectories, Bundles, Evaluation Runs, and Promotion Decisions."""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from brain.improvement.models import TrajectoryRecord, AgentBundleManifest, PairedEvaluationReport
from brain.improvement.storage.blob_store import ContentAddressedBlobStore


class RelationalControlPlaneStore:
    """Manages transactional relational metadata for trajectories, bundles, and promotion audit trails."""

    def __init__(self, db_dir: Optional[Path] = None, blob_store: Optional[ContentAddressedBlobStore] = None):
        self.db_dir = db_dir or Path("d:/Brain/project-brain/reports/improvement/db_store")
        self.db_dir.mkdir(parents=True, exist_ok=True)
        self.blob_store = blob_store or ContentAddressedBlobStore()

        self.trajectories_file = self.db_dir / "trajectories.json"
        self.bundles_file = self.db_dir / "bundles.json"
        self.evaluations_file = self.db_dir / "evaluations.json"

        self._trajectories: Dict[str, Dict] = self._load(self.trajectories_file)
        self._bundles: Dict[str, Dict] = self._load(self.bundles_file)
        self._evaluations: List[Dict] = self._load_list(self.evaluations_file)

    def _load(self, path: Path) -> Dict[str, Dict]:
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _load_list(self, path: Path) -> List[Dict]:
        if not path.exists():
            return []
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return []

    def _save(self, path: Path, data: Any):
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def save_trajectory_header(self, record: TrajectoryRecord) -> str:
        """Saves header metadata in control plane and offloads large events to blob store."""
        data = record.model_dump()
        # Offload raw events to content-addressed blob store
        events_str = json.dumps(data["trajectory"]["events"])
        blob_id = self.blob_store.put_blob(events_str)
        data["trajectory"]["events_blob_id"] = blob_id
        data["trajectory"]["events"] = []  # Compact header representation

        self._trajectories[record.trajectory_id] = data
        self._save(self.trajectories_file, self._trajectories)
        return record.trajectory_id

    def get_trajectory_header(self, trajectory_id: str) -> Optional[TrajectoryRecord]:
        raw = self._trajectories.get(trajectory_id)
        if not raw:
            return None
        return TrajectoryRecord(**raw)

    def save_bundle_manifest(self, bundle: AgentBundleManifest) -> str:
        self._bundles[bundle.bundle_id] = bundle.model_dump()
        self._save(self.bundles_file, self._bundles)
        return bundle.bundle_id

    def get_bundle_manifest(self, bundle_id: str) -> Optional[AgentBundleManifest]:
        raw = self._bundles.get(bundle_id)
        if not raw:
            return None
        return AgentBundleManifest(**raw)

    def record_evaluation(self, report: PairedEvaluationReport) -> int:
        self._evaluations.append(report.model_dump())
        self._save(self.evaluations_file, self._evaluations)
        return len(self._evaluations)
