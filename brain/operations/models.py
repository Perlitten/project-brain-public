"""Models for Night Operations and Incident Management in Project Brain v0.5.3."""

from __future__ import annotations

from typing import List, Optional
from pydantic import BaseModel, Field


class IncidentRecord(BaseModel):
    incident_id: str
    run_id: str
    job_id: str
    affected_repository_ids: List[str] = Field(default_factory=list)
    start_time_utc: str = ""
    end_time_utc: str = ""
    status: str = "open"
    root_error: str = ""
    next_action: str = ""
    cooldown_until_utc: str = ""


class FreshnessReport(BaseModel):
    repository_id: str
    display_name: str
    expected_source_revision: str
    indexed_revision: str
    manifest_revision: str
    graph_revision: str
    vector_generation: str
    freshness_reason: str
    repair_eligibility: bool = True
    recommended_action: str = "reindex"


class VectorRepairJob(BaseModel):
    job_id: str
    repository_id: str
    stale_chunks_count: int
    missing_chunks_count: int
    status: str = "pending"
    post_repair_verified: bool = False


class NightDigest(BaseModel):
    digest_id: str
    timestamp_utc: str = ""
    incidents: List[IncidentRecord] = Field(default_factory=list)
    freshness_reports: List[FreshnessReport] = Field(default_factory=list)
    active_generation_chunks: int = 2963
    stale_repository_count: int = 0
    retrieval_quality_warning: Optional[str] = None
    llm_self_diagnosis_truncated: bool = False
