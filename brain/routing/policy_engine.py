"""Policy Engine for Selective Execution in Project Brain v0.6.0."""

from typing import List, Tuple
from brain.routing.models import (
    ExecutionRoute,
    PolicyMode,
    RouteDecision,
    RoutingContext,
    SelectiveExecutionPolicy,
)


class SelectivePolicyEngine:
    """Evaluates task context and determines execution route according to policy rules."""

    def __init__(self, policy: SelectiveExecutionPolicy | None = None):
        self.policy = policy or SelectiveExecutionPolicy()

    @staticmethod
    def validate_precision_recall_gate(
        precision: float,
        recall: float,
        net_success_delta: int,
        median_score_delta: float,
    ) -> Tuple[bool, str]:
        """v0.6.0 Selective Routing Gate: Route Precision/Recall >= 80% and +2 successes or median +7 score."""
        if precision < 0.80 or recall < 0.80:
            return False, f"Route Precision ({precision:.2%}) or Recall ({recall:.2%}) below mandatory 80.0% threshold"
        if net_success_delta < 2 and median_score_delta < 7.0:
            return False, f"Net task successes (+{net_success_delta}) and median score delta (+{median_score_delta:.1f}) fail minimum threshold (+2 tasks or +7 pts)"
        return True, "v0.6.0 Selective Utility Gate PASSED"

    def evaluate(self, ctx: RoutingContext) -> RouteDecision:
        reasons: List[str] = []
        dominant_signals: List[str] = []
        rejected: List[ExecutionRoute] = []

        # 1. Fail-closed safety checks
        if ctx.evidence_sufficiency == "insufficient" or ctx.task_category == "insufficient_evidence":
            reasons.append("Evidence is insufficient or target module unavailable")
            dominant_signals.append("insufficient_evidence")
            return RouteDecision(
                selected_route=ExecutionRoute.ABSTAIN_STALE,
                brain_route="stale_guard",
                confidence=0.9,
                reasons=reasons,
                rejected_alternatives=[ExecutionRoute.PHASED_BRAIN_SELECTIVE, ExecutionRoute.PHASED_NO_BRAIN],
                dominant_signals=dominant_signals,
                fallback_route=ExecutionRoute.HUMAN_REVIEW_REQUIRED,
            )

        if ctx.source_freshness == "stale" or ctx.graph_freshness == "stale":
            reasons.append("Source revision or graph index is stale")
            dominant_signals.append("stale_source_index")
            return RouteDecision(
                selected_route=ExecutionRoute.ABSTAIN_STALE,
                brain_route="stale_guard",
                confidence=0.95,
                reasons=reasons,
                rejected_alternatives=[ExecutionRoute.PHASED_BRAIN_SELECTIVE],
                dominant_signals=dominant_signals,
                fallback_route=ExecutionRoute.LEGACY_NO_BRAIN,
            )

        if ctx.migration_requirement or ctx.task_complexity == "destructive":
            reasons.append("Task contains destructive or irreversible changes")
            dominant_signals.append("human_review_trigger")
            return RouteDecision(
                selected_route=ExecutionRoute.HUMAN_REVIEW_REQUIRED,
                brain_route="off",
                confidence=1.0,
                reasons=reasons,
                rejected_alternatives=[ExecutionRoute.PHASED_BRAIN_SELECTIVE],
                dominant_signals=dominant_signals,
                human_review_requirement=True,
                fallback_route=ExecutionRoute.HUMAN_REVIEW_REQUIRED,
            )

        # 2. Category-based routing
        cat = ctx.task_category
        target_route = self.policy.category_routes.get(cat, ExecutionRoute.LEGACY_NO_BRAIN)

        # Overrides based on empirical indicators
        if ctx.architecture_sensitivity or ctx.cross_repository_sensitivity:
            target_route = ExecutionRoute.PHASED_BRAIN_SELECTIVE
            dominant_signals.append("architecture_or_cross_repo_sensitivity")
            reasons.append("Task spans architecture or cross-repository boundaries")
        elif ctx.file_count == 1 and ctx.task_complexity == "trivial":
            target_route = ExecutionRoute.LEGACY_NO_BRAIN
            dominant_signals.append("trivial_single_file")
            reasons.append("Single-file trivial edit fast path")

        # Handle PolicyMode
        if ctx.operator_mode == PolicyMode.OBSERVE:
            reasons.append("Policy mode is OBSERVE: logging route recommendation without forcing active override")

        brain_r = "off"
        if target_route == ExecutionRoute.PHASED_BRAIN_SELECTIVE:
            brain_r = "architecture_context" if ctx.architecture_sensitivity else "cross_repo_impact"

        for r in ExecutionRoute:
            if r != target_route:
                rejected.append(r)

        return RouteDecision(
            selected_route=target_route,
            brain_route=brain_r,
            confidence=0.85,
            reasons=reasons,
            rejected_alternatives=rejected[:3],
            dominant_signals=dominant_signals,
            fallback_route=ExecutionRoute.LEGACY_NO_BRAIN,
        )
