"""Single-Lever Candidate Hypothesis Generator for Project Brain v0.8.0."""

import hashlib
import time
from typing import List
from brain.improvement.models import CandidateHypothesis, TrajectoryRecord


class SingleLeverHypothesisGenerator:
    """Ingests failure clusters and synthesizes single-lever challenger candidate hypotheses."""

    ALLOWED_LEVERS = [
        "routing_thresholds",
        "prompt_instructions",
        "evidence_policy",
        "tool_descriptions",
        "context_budgets",
        "verification_sequence",
        "repair_budgets",
    ]

    def generate_hypothesis(
        self,
        cluster_id: str,
        failure_trajectories: List[TrajectoryRecord],
        target_lever: str = "routing_thresholds",
    ) -> CandidateHypothesis:
        """Synthesizes a single-lever CandidateHypothesis from a failure cluster."""
        if target_lever not in self.ALLOWED_LEVERS:
            raise ValueError(f"Lever '{target_lever}' is not in allowed safe levers: {self.ALLOWED_LEVERS}")

        hyp_id = f"hyp-{hashlib.sha256(f'{cluster_id}-{target_lever}-{time.time()}'.encode('utf-8')).hexdigest()[:10]}"

        lever_changes = [
            {
                "lever": target_lever,
                "parameter": "complexity_threshold",
                "old_value": "0.7",
                "new_value": "0.85",
                "rationale": f"Address failure cluster {cluster_id} by raising execution complexity threshold",
            }
        ]

        return CandidateHypothesis(
            candidate_id=f"cnd-{hyp_id}",
            parent_bundle_id="bundle_champion_v070",
            failure_cluster_ids=[cluster_id],
            hypothesis=f"Increasing {target_lever} complexity threshold reduces false-positive selective execution routing for cluster {cluster_id}",
            lever_changes=lever_changes,
            expected_benefit={"target_category": "cross_repo", "predicted_success_gain_pct": 8.0},
            risk_predictions=["slight_token_increase"],
            evaluation_plan_id=f"eval-plan-{hyp_id}",
            author={"system": "brain-hypothesis-factory-v080"},
        )
