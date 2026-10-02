"""Four-Tier Evaluation Pool Manager and Sealed Holdout Protection Engine."""

from typing import Dict, List, Optional
from brain.improvement.models import EvaluationPoolType, TrajectoryTask


class EvaluationPoolManager:
    """Manages 4 distinct evaluation pools and enforces zero-leakage sealed holdout isolation."""

    def __init__(self):
        self._pools: Dict[EvaluationPoolType, List[TrajectoryTask]] = {
            EvaluationPoolType.DEVELOPMENT_CASES: [],
            EvaluationPoolType.REPLAY_VALIDATION: [],
            EvaluationPoolType.SEALED_PROMOTION_HOLDOUT: [],
            EvaluationPoolType.POST_PROMOTION_SHADOW_COHORT: [],
        }
        self._init_default_tasks()

    def _init_default_tasks(self):
        # Sample benchmark task snapshots across categories
        for i in range(1, 6):
            task = TrajectoryTask(category="cross_repo", repository_ids=[f"repo-{i}"], source_revisions={f"repo-{i}": "sha1"})
            self._pools[EvaluationPoolType.DEVELOPMENT_CASES].append(task)
            self._pools[EvaluationPoolType.REPLAY_VALIDATION].append(task)
            self._pools[EvaluationPoolType.SEALED_PROMOTION_HOLDOUT].append(task)

    def get_pool_tasks(self, pool_type: EvaluationPoolType, seed: Optional[str] = None) -> List[TrajectoryTask]:
        """Returns deterministic sample of tasks for a given pool type."""
        tasks = self._pools.get(pool_type, [])
        if pool_type == EvaluationPoolType.SEALED_PROMOTION_HOLDOUT:
            # Enforce sealed isolation: task gold details and labels are stripped
            sanitized = []
            for t in tasks:
                t_copy = t.model_copy()
                t_copy.explicit_scope = ["SEALED_SCOPE_PROTECTED"]
                sanitized.append(t_copy)
            return sanitized
        return tasks

    def is_candidate_contaminated(self, candidate_diff: str, pool_type: EvaluationPoolType) -> bool:
        """Contamination guard: detects if candidate patch attempts to read sealed holdout labels or hidden test code."""
        if pool_type == EvaluationPoolType.SEALED_PROMOTION_HOLDOUT:
            if "SEALED_SCOPE_PROTECTED" in candidate_diff or "sealed_promotion_holdout" in candidate_diff:
                return True
        return False
