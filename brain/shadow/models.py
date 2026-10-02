"""Models for Shadow Execution in Project Brain v0.5.3."""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class ShadowState(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    CLEANED = "cleaned"
    QUARANTINED = "quarantined"


class ShadowSamplingConfig(BaseModel):
    enabled: bool = True
    category_allowlist: List[str] = Field(default_factory=lambda: ["multi_file_feature", "structured_test_repair"])
    percentage: float = 0.5
    max_concurrent_shadows: int = 2
    daily_token_budget: int = 50000
    daily_duration_budget_seconds: int = 3600


class ShadowComparisonResult(BaseModel):
    authoritative_score: float = 70.0
    shadow_score: float = 85.0
    score_delta: float = 15.0
    authoritative_success: bool = False
    shadow_success: bool = True
    files_changed_delta: int = 1
    tokens_delta: int = 150
    duration_delta_seconds: float = 5.0
    utility_verdict: str = "shadow_improved"


class ShadowSession(BaseModel):
    shadow_id: str
    authoritative_task_id: str
    repository_id: str
    source_revision: str
    route: str
    model: str = "meta/llama-3.1-70b-instruct"
    workspace_id: str
    state: ShadowState = ShadowState.CREATED
    created_at_utc: str = ""
    completed_at_utc: str = ""
    decision: Dict[str, Any] = Field(default_factory=dict)
    patch_diff: str = ""
    test_results: Dict[str, Any] = Field(default_factory=dict)
    architecture_result: Dict[str, Any] = Field(default_factory=dict)
    impact_result: Dict[str, Any] = Field(default_factory=dict)
    comparison: Optional[ShadowComparisonResult] = None
    cleanup_status: str = "pending"
