"""Data models for Champion-Anchored Tournament League, Resource Quotas, and Lineage (v0.9.0)."""

from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field
from brain.improvement.models import PairedEvaluationReport


class TournamentStatus(str, Enum):
    LEAGUE_COMPLETED = "league_completed"
    FINALIST_SELECTED = "finalist_selected"
    SEALED_T3_PASSED = "sealed_t3_passed"
    SEALED_T3_FAILED = "sealed_t3_failed"
    WINNER_PROMOTED = "winner_promoted"
    CHAMPION_RETAINED = "champion_retained"


class CandidateEntry(BaseModel):
    bundle_id: str
    parent_bundle_id: str = "bundle_champion_v070"
    is_champion: bool = False
    candidate_hypothesis_id: Optional[str] = None
    mutation_id: Optional[str] = None
    bundle_digest: Optional[str] = None
    generation: int = 1


class CandidateComparison(BaseModel):
    champion_bundle_id: str
    challenger_bundle_id: str
    evaluation_pool_type: str = "development_replay"
    evaluation_report: PairedEvaluationReport


class TournamentSelection(BaseModel):
    finalist_bundle_id: Optional[str] = None
    selection_rationale: str
    multiplicity_corrected_p_value: float = 0.05
    sealed_t3_evaluated: bool = False
    sealed_t3_passed: bool = False


class TournamentResourceContract(BaseModel):
    per_candidate_concurrency: int = 1
    cpu_quota: str = "2.0"
    memory_quota: str = "4GB"
    request_quota_per_min: int = 120
    isolated_cache_namespace: str
    isolated_workspace_namespace: str


class TournamentRun(BaseModel):
    tournament_id: str
    status: TournamentStatus
    champion_bundle_id: str
    finalist_bundle_id: Optional[str] = None
    winning_bundle_id: str
    candidates: List[CandidateEntry]
    league_comparisons: List[CandidateComparison]
    finalist_selection: TournamentSelection
    sealed_t3_report: Optional[PairedEvaluationReport] = None
    factory_attestation_hash: str
    decision_reports: List[str] = Field(default_factory=list)
    created_at_utc: str
