from typing import Optional, List, Dict, Any, Literal
from pydantic import BaseModel, Field


class IndexRequest(BaseModel):
    repo_path: str


class AskRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=4000)
    repo_path: Optional[str] = Field(default=None, max_length=1000)
    # Retrieval is English-and-code only, so a caller asking in another language
    # can supply a translated query for the search while `query` — the question
    # as actually asked — still reaches the model, which then answers in that
    # language without a second translation round-trip.
    retrieval_query: Optional[str] = Field(default=None, max_length=4000)
    include_debug: bool = False


class ContextRequest(BaseModel):
    task_description: str = Field(..., min_length=1, max_length=4000)
    repo_path: Optional[str] = Field(default=None, max_length=1000)
    # Runtime context is intentionally ephemeral. Durable handoff remains the
    # explicit deep job surface so a normal read cannot write to disk/DB.
    persist: bool = False
    max_tokens: int = Field(default=3500, ge=500, le=8000)
    include_debug: bool = False


class ImpactRequest(BaseModel):
    change_request: str = Field(..., min_length=1, max_length=4000)
    repo_path: Optional[str] = Field(default=None, max_length=1000)
    include_debug: bool = False
    # Caps the combined directly+indirectly affected lists (ranked by
    # relevance). Default 75 (25 direct / 50 indirect). 0 = no truncation.
    max_results: Optional[int] = Field(default=None, ge=0, le=10000)
    # Fast mode skips both LLM calls (deterministic keywords + rationale),
    # cutting latency from ~4 min to ~30 s. Ranked lists and the deterministic
    # risk score are unaffected.
    fast: bool = False


class DiffReviewRequest(BaseModel):
    base: Optional[str] = None
    head: Optional[str] = "current"
    repo_path: Optional[str] = None


class LearningCreate(BaseModel):
    statement: str
    category: Optional[str] = None
    confidence: float = 0.5
    repo_scope: Optional[str] = None
    valid_until: Optional[str] = None


class EpisodeSearchRequest(BaseModel):
    query: str = Field(..., min_length=1)
    limit: int = Field(10, ge=1, le=50)
    status: Optional[Literal["pending", "promoted", "rejected", "duplicate", "merged"]] = None


class EpisodeDecisionRequest(BaseModel):
    reason: Optional[str] = Field(None, max_length=2000)


class SkillCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    description: str = Field(..., min_length=1)
    triggers: List[str] = Field(default_factory=list)
    workflow: List[Dict[str, Any]] = Field(default_factory=list)
    source_episode_ids: List[int] = Field(default_factory=list)
    source_learning_ids: List[int] = Field(default_factory=list)
    confidence: float = Field(0.5, ge=0.0, le=1.0)
    repo_scope: Optional[str] = None


class SkillMatchRequest(BaseModel):
    query: str = Field(..., min_length=1)
    limit: int = Field(5, ge=1, le=20)


class DecisionCreate(BaseModel):
    title: str
    repo_path: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = "active"
    date: Optional[str] = None
    reason: Optional[str] = None
    consequences: Optional[str] = None
    affected_features: Optional[List[str]] = None
    affected_modules: Optional[List[str]] = None
    affected_files: Optional[List[str]] = None


class RuleCreate(BaseModel):
    name: str
    repo_path: Optional[str] = None
    description: Optional[str] = None
    type: Optional[str] = "architecture"
    severity: Optional[str] = "medium"
    status: Optional[str] = "active"
    applies_to: Optional[Dict[str, Any]] = None
    rule_id: Optional[str] = None


class FeatureCreate(BaseModel):
    name: str
    properties: Optional[Dict[str, Any]] = None
    repo_path: Optional[str] = None


class ReindexJobRequest(BaseModel):
    repo_path: Optional[str] = None
    clean: bool = False
    source_revision: Optional[str] = Field(default=None, max_length=128)
    verify_after: bool = True
    benchmark_after: bool = False
    idempotency_key: Optional[str] = Field(default=None, max_length=255)


class EmbeddingVerifyRequest(BaseModel):
    repo_path: Optional[str] = None


class EmbeddingBackfillRequest(BaseModel):
    repo_path: Optional[str] = None
    pgvector_only: bool = False
    limit: Optional[int] = Field(default=None, ge=1, le=10000)
    batch_size: int = Field(default=8, ge=1, le=64)
    provider: Optional[str] = None


class BenchmarkJobRequest(BaseModel):
    repo_path: Optional[str] = None
    golden: Optional[str] = None
    smoke: bool = False


class ProactiveInsightsJobRequest(BaseModel):
    repo_path: Optional[str] = None
    use_llm: Optional[bool] = None
    scheduled: bool = False


class MemoryConsolidationJobRequest(BaseModel):
    scheduled: bool = False
    require_approval: Optional[bool] = None
    dry_run: bool = False


class SelfDiagnosisJobRequest(BaseModel):
    repo_path: Optional[str] = None
    use_llm: Optional[bool] = None
    scheduled: bool = False
    idempotency_key: Optional[str] = Field(default=None, max_length=255)
    trigger_source: Optional[str] = Field(default=None, max_length=64)
    trigger_summary: Optional[str] = Field(default=None, max_length=1000)
    trigger_reference: Optional[str] = Field(default=None, max_length=255)


class NightlyMaintenanceJobRequest(BaseModel):
    repo_path: Optional[str] = Field(default=None, max_length=1000)
    scheduled: bool = False
    notify: bool = True
    idempotency_key: Optional[str] = Field(default=None, max_length=255)


class DeepContextJobRequest(BaseModel):
    task_description: str = Field(..., min_length=1, max_length=4000)
    repo_path: Optional[str] = Field(default=None, max_length=1000)
    notify: bool = True
    idempotency_key: Optional[str] = Field(default=None, max_length=255)


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=1000)
    repo_path: Optional[str] = Field(default=None, max_length=1000)
    # Bounded so a caller can't force an unbounded vector scan.
    limit: int = Field(default=5, ge=1, le=50)
    max_tokens: int = Field(default=600, ge=100, le=600)
    include_debug: bool = False
    response_mode: Literal["locator", "legacy"] = "locator"


class RelatedRequest(BaseModel):
    file_path: str = Field(..., min_length=1, max_length=1000)
    repo_path: Optional[str] = Field(default=None, max_length=1000)
