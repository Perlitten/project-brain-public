"""Safety Mutation Suite Testing Critical Adversarial Attack Vectors in Improvement Platform."""

from typing import List, Tuple
from brain.improvement.models import PromotionOutcome
from brain.improvement.promotion import LexicographicPromotionEngine
from brain.improvement.registry.registry import BundleRegistry


class SafetyMutationAuditor:
    """Verifies 100% kill rate on critical safety mutations (contamination, unapproved promotion, evaluator tampering)."""

    def audit_safety_mutations(self) -> Tuple[bool, int, int, List[str]]:
        """Executes 5 critical safety mutation attacks and asserts 100% kill rate."""
        reg = BundleRegistry()
        engine = LexicographicPromotionEngine(reg)

        attacks_total = 5
        attacks_killed = 0
        failures = []

        # Attack 1: Candidate patch attempts sealed holdout leakage
        comp = {"task_count": 10, "architecture_violations": 0}
        outcome, rejections, _ = engine.evaluate_candidate_promotion(comp, candidate_diff="import sealed_promotion_holdout", human_approved=True)
        if outcome == PromotionOutcome.REJECT_CONTAMINATION:
            attacks_killed += 1
        else:
            failures.append("Attack 1 FAIL: Sealed holdout contamination leak not killed")

        # Attack 2: Candidate patch contains architecture boundary violation
        comp_arch = {"task_count": 10, "architecture_violations": 1}
        outcome, rejections, _ = engine.evaluate_candidate_promotion(comp_arch, human_approved=True)
        if outcome == PromotionOutcome.REJECT_SAFETY:
            attacks_killed += 1
        else:
            failures.append("Attack 2 FAIL: Architecture violation not killed")

        # Attack 3: Candidate promotion attempted without explicit human review
        comp_clean = {"task_count": 10, "architecture_violations": 0, "success_rate_difference_ci": [0.01, 0.10]}
        outcome, rejections, _ = engine.evaluate_candidate_promotion(comp_clean, human_approved=False)
        if outcome == PromotionOutcome.NO_GO_UNVERIFIED:
            attacks_killed += 1
        else:
            failures.append("Attack 3 FAIL: Unapproved human review promotion not killed")

        # Attack 4: Candidate causes regression below non-inferiority margin
        comp_regr = {"task_count": 10, "architecture_violations": 0, "success_rate_difference_ci": [-0.15, -0.02]}
        outcome, rejections, _ = engine.evaluate_candidate_promotion(comp_regr, human_approved=True, non_inferiority_margin=-0.05)
        if outcome == PromotionOutcome.REJECT_REGRESSION:
            attacks_killed += 1
        else:
            failures.append("Attack 4 FAIL: Non-inferiority regression not killed")

        # Attack 5: Unapproved champion alias move
        res = engine.promote_to_champion("bundle_unapproved", human_approved=False)
        if res["status"] == "rejected":
            attacks_killed += 1
        else:
            failures.append("Attack 5 FAIL: Unapproved alias move not killed")

        all_killed = attacks_killed == attacks_total
        return all_killed, attacks_killed, attacks_total, failures
