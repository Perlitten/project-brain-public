"""Local Review Queue Persistence and Lifecycle Manager (Workstream D)."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, List, Optional


@dataclass
class ReviewItem:
    review_id: str
    repository: str
    base_revision: str
    candidate_revision: str
    decision: str
    risk_category: str
    risk_score: float
    owners: List[str]
    created_at_utc: str
    updated_at_utc: str
    state: str  # 'pending', 'acknowledged', 'approved', 'rejected', 'superseded'
    package_path: str
    run_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ReviewQueueManager:
    """Manages persistence and state transitions for architectural review queue."""

    def __init__(self, queue_file_path: Path):
        self.file_path = queue_file_path.resolve()

    def _load_all(self) -> List[ReviewItem]:
        if not self.file_path.exists():
            return []
        items: List[ReviewItem] = []
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        d = json.loads(line)
                        items.append(ReviewItem(**d))
        except Exception:
            return []
        return items

    def _save_all(self, items: List[ReviewItem]) -> None:
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.file_path.with_suffix(".tmp")
        with open(temp_path, "w", encoding="utf-8") as f:
            for item in items:
                f.write(json.dumps(item.to_dict()) + "\n")
        temp_path.replace(self.file_path)

    def enqueue_review(
        self,
        repository: str,
        base_rev: str,
        cand_rev: str,
        decision: str,
        risk_category: str,
        risk_score: float,
        owners: List[str],
        package_path: str,
        run_fingerprint: str,
    ) -> ReviewItem:
        items = self._load_all()
        now_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        # Supersede any previous pending/acknowledged reviews with the same run fingerprint
        for item in items:
            if item.run_fingerprint == run_fingerprint and item.state in {"pending", "acknowledged"}:
                item.state = "superseded"
                item.updated_at_utc = now_str

        review_id = f"rev-{run_fingerprint[:8]}-{int(time.time())}"
        new_item = ReviewItem(
            review_id=review_id,
            repository=repository,
            base_revision=base_rev,
            candidate_revision=cand_rev,
            decision=decision,
            risk_category=risk_category,
            risk_score=risk_score,
            owners=owners,
            created_at_utc=now_str,
            updated_at_utc=now_str,
            state="pending",
            package_path=package_path,
            run_fingerprint=run_fingerprint,
        )
        items.append(new_item)
        self._save_all(items)
        return new_item

    def list_reviews(self, state: Optional[str] = None) -> List[ReviewItem]:
        items = self._load_all()
        if state:
            return [i for i in items if i.state == state]
        return items

    def get_review(self, review_id: str) -> Optional[ReviewItem]:
        for i in self._load_all():
            if i.review_id == review_id:
                return i
        return None

    def update_state(self, review_id: str, new_state: str) -> Optional[ReviewItem]:
        items = self._load_all()
        target: Optional[ReviewItem] = None
        now_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        for item in items:
            if item.review_id == review_id:
                item.state = new_state
                item.updated_at_utc = now_str
                target = item
                break

        if target:
            self._save_all(items)
        return target
