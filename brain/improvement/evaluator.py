"""Deterministic Trajectory Evaluator and Invariant Validator."""

from typing import List, Tuple
from brain.improvement.models import TrajectoryRecord


class TrajectoryEvaluator:
    """Evaluates trajectory records against invariants and computes diagnostic scores."""

    def evaluate_trajectory(self, record: TrajectoryRecord) -> Tuple[bool, float, List[str]]:
        """Evaluates trajectory record and returns (binary_success, diagnostic_score, failure_modes)."""
        failure_modes = []

        # Invariant 1: Exit code / execution checks
        if record.outcome.failure_modes:
            failure_modes.extend(record.outcome.failure_modes)

        # Invariant 2: Cost boundary check
        if record.cost.model_input_tokens > 500000:
            failure_modes.append("excessive_token_usage")

        # Invariant 3: Privacy check
        if record.privacy.classification.value == "restricted_vault" and record.privacy.training_eligible:
            failure_modes.append("privacy_violation_training_eligible_on_restricted")

        binary_success = record.outcome.binary_success and len(failure_modes) == 0

        # Calculate diagnostic score
        score = record.outcome.diagnostic_score
        if score == 0.0:
            score = 100.0 if binary_success else 50.0

        return binary_success, score, failure_modes
