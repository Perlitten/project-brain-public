"""Data models for Autonomous Self-Healing Engineering Operator (v0.9.0)."""
from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field
import time


class GoalPhase(str, Enum):
    GOAL_RECEIVED = "goal_received"
    INTERNAL_PLANNING = "internal_planning"
    VERIFIED_CONTEXT_BUILT = "verified_context_built"
    EXECUTION = "execution"
    TESTING = "testing"
    DIAGNOSIS = "diagnosis"
    REPAIRING = "repairing"
    RETESTING = "retesting"
    COMMITTING = "committing"
    DEPLOYING = "deploying"
    VERIFYING_RUNTIME = "verifying_runtime"
    CLOSED = "closed"
    BLOCKED_ESCALATED = "blocked_escalated"


class EscalationReason(str, Enum):
    UNAVAILABLE_CREDENTIALS = "unavailable_credentials"
    DESTRUCTIVE_PRODUCTION_ACTION = "destructive_production_action"
    CONTRADICTORY_REQUIREMENTS = "contradictory_requirements"
    HARD_SAFETY_BOUNDARY = "hard_safety_boundary"


class TelemetryRecord(BaseModel):
    call_id: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int = 0
    total_tokens: int
    api_cost_usd: float
    latency_ms: float
    timestamp_utc: str = Field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))


class EscalationRecord(BaseModel):
    escalation_id: str
    reason: EscalationReason
    description: str
    provenance_proof: str
    timestamp_utc: str = Field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))


class ExecutionCheckpoint(BaseModel):
    checkpoint_id: str
    phase: GoalPhase
    attempt_count: int = 1
    passed_tests: List[str] = Field(default_factory=list)
    failed_tests: List[str] = Field(default_factory=list)
    patch_id: Optional[str] = None
    created_at_utc: str = Field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))


class GoalSession(BaseModel):
    session_id: str
    goal_description: str
    repository_id: str = "project-brain"
    current_phase: GoalPhase = GoalPhase.GOAL_RECEIVED
    checkpoints: List[ExecutionCheckpoint] = Field(default_factory=list)
    telemetry: List[TelemetryRecord] = Field(default_factory=list)
    escalations: List[EscalationRecord] = Field(default_factory=list)
    is_closed: bool = False
    is_blocked: bool = False
    created_at_utc: str = Field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    updated_at_utc: str = Field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
