"""Canonical Trajectory, Expanded AgentBundle, Evaluation Pool, and Promotion Models for v0.7.1."""

from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class PrivacyClassification(str, Enum):
    PUBLIC = "public"
    INTERNAL_CONFIDENTIAL = "internal_confidential"
    RESTRICTED_VAULT = "restricted_vault"
    QUARANTINED = "quarantined"


class PromotionOutcome(str, Enum):
    PROMOTE = "PROMOTE"
    REJECT_REGRESSION = "REJECT_REGRESSION"
    REJECT_SAFETY = "REJECT_SAFETY"
    REJECT_COST = "REJECT_COST"
    REJECT_CONTAMINATION = "REJECT_CONTAMINATION"
    NO_GO_INSUFFICIENT_EVIDENCE = "NO_GO_INSUFFICIENT_EVIDENCE"
    NO_GO_UNVERIFIED = "NO_GO_UNVERIFIED"
    RETAIN_FOR_RESEARCH = "RETAIN_FOR_RESEARCH"


class EvaluationPoolType(str, Enum):
    DEVELOPMENT_CASES = "development_cases"
    REPLAY_VALIDATION = "replay_validation"
    SEALED_PROMOTION_HOLDOUT = "sealed_promotion_holdout"
    POST_PROMOTION_SHADOW_COHORT = "post_promotion_shadow_cohort"


class TrajectoryTask(BaseModel):
    category: str
    repository_ids: List[str]
    source_revisions: Dict[str, str]
    explicit_scope: List[str] = Field(default_factory=list)
    risk_class: str = "medium"
    contains_user_content: bool = True


class TrajectoryRouting(BaseModel):
    selected_route: str
    rejected_routes: List[str] = Field(default_factory=list)
    confidence: float
    signals: Dict[str, Any] = Field(default_factory=dict)
    policy_version: str = "routing-policy-v4"


class TrajectoryBody(BaseModel):
    events: List[Dict[str, Any]] = Field(default_factory=list)
    plans: List[Dict[str, Any]] = Field(default_factory=list)
    checkpoints: List[str] = Field(default_factory=list)
    tool_calls: List[Dict[str, Any]] = Field(default_factory=list)
    test_runs: List[Dict[str, Any]] = Field(default_factory=list)
    repairs: List[Dict[str, Any]] = Field(default_factory=list)
    patches: List[Dict[str, Any]] = Field(default_factory=list)


class TrajectoryOutcome(BaseModel):
    binary_success: bool = False
    diagnostic_score: float = 0.0
    human_disposition: str = "pending_review"
    failure_modes: List[str] = Field(default_factory=list)


class TrajectoryCost(BaseModel):
    model_input_tokens: int = 0
    model_output_tokens: int = 0
    brain_evidence_tokens: int = 0
    provider_retries: int = 0
    wall_time_ms: float = 0.0


class TrajectoryPrivacy(BaseModel):
    classification: PrivacyClassification = PrivacyClassification.INTERNAL_CONFIDENTIAL
    redaction_version: str = "redaction-v3"
    raw_retention_until: str = "UNSPECIFIED"
    training_eligible: bool = False  # Default fail-closed policy


class TrajectoryRecord(BaseModel):
    trajectory_id: str
    trace_id: Optional[str] = None
    task_id: str
    session_id: str
    parent_trajectory_id: Optional[str] = None
    agent_bundle_id: str
    champion_bundle_id: str
    candidate_id: Optional[str] = None
    execution_mode: str = "authoritative_legacy"
    task: TrajectoryTask
    routing: TrajectoryRouting
    trajectory: TrajectoryBody
    outcome: TrajectoryOutcome
    cost: TrajectoryCost
    privacy: TrajectoryPrivacy = Field(default_factory=TrajectoryPrivacy)
    attestation_ids: List[str] = Field(default_factory=list)


# 10-Layer Expanded Content-Addressed AgentBundleManifest
class AgentBundleManifest(BaseModel):
    schema_version: int = 1
    bundle_id: str  # sha256 of canonical manifest content
    identity: Dict[str, Any]
    model: Dict[str, Any]
    routing: Dict[str, Any]
    prompts: Dict[str, Any]
    evidence: Dict[str, Any]
    tools: Dict[str, Any]
    execution: Dict[str, Any]
    verification: Dict[str, Any]
    safety: Dict[str, Any]
    compatibility: Dict[str, Any]
    slsa_provenance_uri: Optional[str] = None
    rekor_transparency_uuid: Optional[str] = None


class PairedEvaluationReport(BaseModel):
    champion_bundle_id: str
    challenger_bundle_id: str
    evaluation_pool_type: EvaluationPoolType
    task_count: int
    paired_success_concordance: Dict[str, int]  # win_win, loss_loss, champ_win_challenger_loss, challenger_win_champ_loss
    mcnemar_p_value: float
    success_rate_difference_ci: List[float]  # [lower_bound, upper_bound]
    median_score_delta_ci: List[float]
    non_inferiority_margin: float = -0.05
    non_inferiority_passed: bool
    outcome: PromotionOutcome
    gate_rejections: List[str] = Field(default_factory=list)


class CandidateHypothesis(BaseModel):
    candidate_id: str
    parent_bundle_id: str
    failure_cluster_ids: List[str]
    hypothesis: str
    lever_changes: List[Dict[str, str]]
    expected_benefit: Dict[str, Any]
    risk_predictions: List[str]
    evaluation_plan_id: str
    author: Dict[str, str]
