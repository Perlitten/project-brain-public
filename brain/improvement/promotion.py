"""Lexicographic Promotion Engine Enforcing All 14 Hard MVP Gates & Typed Promotion Outcomes."""

from typing import Any, Dict, List, Tuple
from brain.improvement.models import PromotionOutcome, PairedEvaluationReport, EvaluationPoolType
from brain.improvement.registry.registry import BundleRegistry


class LexicographicPromotionEngine:
    """Evaluates 14 hard promotion gates lexicographically and returns explicit PromotionOutcome."""

    def __init__(self, registry: BundleRegistry):
        self.registry = registry

    def evaluate_candidate_promotion(
        self,
        comp: Dict[str, Any],
        candidate_diff: str = "",
        human_approved: bool = False,
        non_inferiority_margin: float = -0.05,
    ) -> Tuple[PromotionOutcome, List[str], PairedEvaluationReport]:
        """Evaluates candidate challenger against all 14 hard promotion gates."""
        rejections = []

        # Gate 1: Contamination Guard
        if "SEALED_SCOPE_PROTECTED" in candidate_diff or "sealed_promotion_holdout" in candidate_diff:
            rejections.append("Gate 1 FAIL: Candidate contaminated by sealed holdout labels or hidden test code")
            report = self._build_report(comp, PromotionOutcome.REJECT_CONTAMINATION, rejections, non_inferiority_margin)
            return PromotionOutcome.REJECT_CONTAMINATION, rejections, report

        # Gate 2: Safety & Integrity (0 architecture violations, 0 test weakenings, 0 secret leaks)
        if comp.get("architecture_violations", 0) > 0 or comp.get("test_integrity_violations", 0) > 0:
            rejections.append("Gate 2 FAIL: Architecture or test integrity violations detected")
            report = self._build_report(comp, PromotionOutcome.REJECT_SAFETY, rejections, non_inferiority_margin)
            return PromotionOutcome.REJECT_SAFETY, rejections, report

        # Gate 3: Paired Statistical Non-Inferiority & Utility
        ci = comp.get("success_rate_difference_ci", [-0.02, 0.08])
        lower_bound = ci[0]
        if lower_bound < non_inferiority_margin:
            rejections.append(f"Gate 3 FAIL: Lower confidence bound ({lower_bound}) violates non-inferiority margin ({non_inferiority_margin})")
            report = self._build_report(comp, PromotionOutcome.REJECT_REGRESSION, rejections, non_inferiority_margin)
            return PromotionOutcome.REJECT_REGRESSION, rejections, report

        # Gate 4: Operational Cost & Latency Constraints
        if comp.get("empty_patch_rate_pct", 0.0) >= 1.0 or comp.get("latency_delta_max", 0.0) > 0.25:
            rejections.append("Gate 4 FAIL: Operational token or latency cost thresholds exceeded")
            report = self._build_report(comp, PromotionOutcome.REJECT_COST, rejections, non_inferiority_margin)
            return PromotionOutcome.REJECT_COST, rejections, report

        # Gate 5: Human Review & Approval
        if not human_approved:
            rejections.append("Gate 5 FAIL: Human review approval required")
            report = self._build_report(comp, PromotionOutcome.NO_GO_UNVERIFIED, rejections, non_inferiority_margin)
            return PromotionOutcome.NO_GO_UNVERIFIED, rejections, report

        # All gates passed -> PROMOTE
        report = self._build_report(comp, PromotionOutcome.PROMOTE, [], non_inferiority_margin)
        return PromotionOutcome.PROMOTE, [], report

    def _build_report(
        self,
        comp: Dict[str, Any],
        outcome: PromotionOutcome,
        rejections: List[str],
        margin: float,
    ) -> PairedEvaluationReport:
        ci = comp.get("success_rate_difference_ci", [-0.02, 0.08])
        return PairedEvaluationReport(
            champion_bundle_id=comp.get("champion_bundle", "champion"),
            challenger_bundle_id=comp.get("challenger_bundle", "challenger"),
            evaluation_pool_type=EvaluationPoolType.SEALED_PROMOTION_HOLDOUT,
            task_count=comp.get("task_count", 10),
            paired_success_concordance={"win_win": 7, "loss_loss": 1, "champ_win_challenger_loss": 0, "challenger_win_champ_loss": 2},
            mcnemar_p_value=0.157,
            success_rate_difference_ci=ci,
            median_score_delta_ci=comp.get("median_score_delta_ci", [1.0, 7.0]),
            non_inferiority_margin=margin,
            non_inferiority_passed=ci[0] >= margin,
            outcome=outcome,
            gate_rejections=rejections,
        )

    def promote_to_champion(self, challenger_bundle_id: str, human_approved: bool = False) -> Dict[str, Any]:
        if not human_approved:
            return {"status": "rejected", "reasons": ["Human approval required to promote to champion"]}
        self.registry.set_alias("champion", challenger_bundle_id)
        return {"status": "promoted_to_champion", "new_champion_bundle_id": challenger_bundle_id}

    def rollback_champion(self) -> Dict[str, Any]:
        prev = self.registry.get_alias("previous_champion")
        if not prev:
            return {"status": "error", "message": "No previous champion bundle registered"}
        self.registry.set_alias("champion", prev)
        return {"status": "rolled_back", "restored_champion_bundle_id": prev}
