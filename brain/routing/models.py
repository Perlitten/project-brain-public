"""Task Utility Router and Selective Execution Policy Data Models in Project Brain.

Strict schemas for task assessment, repository complexity metrics,
routing decisions, and selective execution policy configuration.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


# --- Legacy v0.5.1 Task Utility Router Models ---

class TaskRouteEnum(str, Enum):
    NO_BRAIN = "no_brain"
    QUICK_SYMBOL_SEARCH = "quick_symbol_search"
    TARGETED_CONTEXT_PACK = "targeted_context_pack"
    ARCHITECTURE_CONTEXT = "architecture_context"
    IMPACT_ANALYSIS = "impact_analysis"
    CROSS_REPO_IMPACT = "cross_repo_impact"
    REMEDIATION_CONTEXT = "remediation_context"
    REPOSITORY_ONBOARDING = "repository_onboarding"
    ABSTAIN_STALE = "abstain_stale"
    ABSTAIN_UNSUPPORTED = "abstain_unsupported"


class RoutingModeEnum(str, Enum):
    OFF = "off"
    OBSERVE = "observe"
    RECOMMEND = "recommend"
    AUTOMATIC = "automatic"


@dataclass
class TaskAssessment:
    task_id: Optional[str] = None
    prompt: str = ""
    file_references: List[str] = field(default_factory=list)
    repository_count: int = 1
    total_file_count: int = 0
    subsystem_count: int = 0
    expected_affected_files: int = 1
    architecture_sensitive: bool = False
    dependency_sensitive: bool = False
    cross_repo_sensitive: bool = False
    unfamiliar_repo: bool = False
    graph_available: bool = True
    graph_fresh: bool = True
    expected_search_cost_tokens: int = 500
    expected_brain_benefit_score: float = 0.5

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class LegacyRouteDecision:
    route: TaskRouteEnum
    confidence: float
    reasons: List[str]
    expected_brain_calls: int = 0
    max_evidence_tokens: int = 2400
    max_latency_ms: int = 5000
    required_freshness: bool = True
    fallback_behavior: str = "ordinary_repository_tools"

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["route"] = self.route.value
        return d


# --- v0.5.3 Selective Execution Policy Models ---

class ExecutionRoute(str, Enum):
    LEGACY_NO_BRAIN = "legacy_no_brain"
    LEGACY_WITH_OPTIONAL_BRAIN = "legacy_with_optional_brain"
    PHASED_NO_BRAIN = "phased_no_brain"
    PHASED_BRAIN_SELECTIVE = "phased_brain_selective"
    PHASED_SHADOW = "phased_shadow"
    ABSTAIN_STALE = "abstain_stale"
    ABSTAIN_UNSUPPORTED = "abstain_unsupported"
    HUMAN_REVIEW_REQUIRED = "human_review_required"


class PolicyMode(str, Enum):
    OFF = "off"
    OBSERVE = "observe"
    SHADOW = "shadow"
    SELECTIVE = "selective"


class RoutingContext(BaseModel):
    task_category: str = "general"
    task_complexity: str = "moderate"
    repository_count: int = 1
    file_count: int = 1
    subsystem_count: int = 1
    cross_repository_sensitivity: bool = False
    architecture_sensitivity: bool = False
    expected_changed_file_count: int = 1
    failing_test_available: bool = False
    migration_requirement: bool = False
    source_freshness: str = "fresh"
    graph_freshness: str = "fresh"
    vector_freshness: str = "fresh"
    evidence_sufficiency: str = "sufficient"
    provider_health: str = "healthy"
    available_budget_tokens: int = 100000
    operator_mode: PolicyMode = PolicyMode.OBSERVE


class RouteDecision(BaseModel):
    selected_route: ExecutionRoute = ExecutionRoute.LEGACY_NO_BRAIN
    brain_route: str = "off"
    confidence: float = 1.0
    reasons: List[str] = Field(default_factory=list)
    rejected_alternatives: List[ExecutionRoute] = Field(default_factory=list)
    dominant_signals: List[str] = Field(default_factory=list)
    threshold_values: Dict[str, Any] = Field(default_factory=dict)
    freshness_state: str = "fresh"
    expected_cost_tokens: int = 1000
    expected_benefit_score: float = 75.0
    policy_version: str = "v0.5.3"
    required_freshness: Any = "fresh"
    phase_budgets: Dict[str, int] = Field(default_factory=lambda: {"investigation": 300, "planning": 300, "implementation": 600})
    repair_budget: int = 3
    verification_depth: str = "standard"
    shadow_requirement: bool = False
    human_review_requirement: bool = False
    fallback_route: ExecutionRoute = ExecutionRoute.LEGACY_NO_BRAIN
    # Backward compatibility fields for BrainTaskRouter
    route: Optional[TaskRouteEnum] = None
    expected_brain_calls: int = 0
    max_evidence_tokens: int = 2400
    max_latency_ms: int = 5000
    fallback_behavior: str = "ordinary_repository_tools"

    def to_dict(self) -> Dict[str, Any]:
        d = self.model_dump()
        if self.route is not None:
            d["route"] = self.route.value
        return d


class SelectiveExecutionPolicy(BaseModel):
    policy_version: str = "v0.5.3"
    mode: PolicyMode = PolicyMode.OBSERVE
    category_routes: Dict[str, ExecutionRoute] = Field(
        default_factory=lambda: {
            "trivial_localized": ExecutionRoute.LEGACY_NO_BRAIN,
            "non_obvious_bug": ExecutionRoute.PHASED_NO_BRAIN,
            "multi_file_feature": ExecutionRoute.PHASED_BRAIN_SELECTIVE,
            "schema_migration": ExecutionRoute.PHASED_NO_BRAIN,
            "architecture_boundary": ExecutionRoute.PHASED_BRAIN_SELECTIVE,
            "cross_repo_contract": ExecutionRoute.PHASED_BRAIN_SELECTIVE,
            "misleading_symptom": ExecutionRoute.PHASED_NO_BRAIN,
            "competing_remediations": ExecutionRoute.PHASED_BRAIN_SELECTIVE,
            "structured_test_repair": ExecutionRoute.PHASED_NO_BRAIN,
            "insufficient_evidence": ExecutionRoute.ABSTAIN_STALE,
        }
    )
