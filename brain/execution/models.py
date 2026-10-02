"""Typed Execution Session, Contract, and State Machine Models for Project Brain v0.5.2."""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Set


class ExecutionState(str, Enum):
    RECEIVED = "received"
    ASSESSING = "assessing"
    INVESTIGATING = "investigating"
    PLANNING = "planning"
    PLAN_READY = "plan_ready"
    PLAN_REJECTED = "plan_rejected"
    IMPLEMENTING = "implementing"
    TESTING = "testing"
    REPAIRING = "repairing"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    BUDGET_EXHAUSTED = "budget_exhausted"
    STALE = "stale"


# Terminal states from which silent re-entry into implementation is forbidden
TERMINAL_STATES = {
    ExecutionState.COMPLETED,
    ExecutionState.FAILED,
    ExecutionState.CANCELLED,
    ExecutionState.TIMED_OUT,
    ExecutionState.BUDGET_EXHAUSTED,
    ExecutionState.STALE,
}


# Valid transitions graph (Fails Closed on invalid state movements)
VALID_TRANSITIONS: Dict[ExecutionState, Set[ExecutionState]] = {
    ExecutionState.RECEIVED: {ExecutionState.ASSESSING, ExecutionState.CANCELLED},
    ExecutionState.ASSESSING: {ExecutionState.INVESTIGATING, ExecutionState.CANCELLED, ExecutionState.FAILED},
    ExecutionState.INVESTIGATING: {ExecutionState.PLANNING, ExecutionState.FAILED, ExecutionState.CANCELLED, ExecutionState.BUDGET_EXHAUSTED, ExecutionState.TIMED_OUT},
    ExecutionState.PLANNING: {ExecutionState.PLAN_READY, ExecutionState.PLAN_REJECTED, ExecutionState.FAILED, ExecutionState.CANCELLED, ExecutionState.BUDGET_EXHAUSTED, ExecutionState.TIMED_OUT},
    ExecutionState.PLAN_READY: {ExecutionState.IMPLEMENTING, ExecutionState.PLANNING, ExecutionState.CANCELLED, ExecutionState.FAILED},
    ExecutionState.PLAN_REJECTED: {ExecutionState.PLANNING, ExecutionState.INVESTIGATING, ExecutionState.FAILED, ExecutionState.CANCELLED},
    ExecutionState.IMPLEMENTING: {ExecutionState.TESTING, ExecutionState.REPAIRING, ExecutionState.FAILED, ExecutionState.CANCELLED, ExecutionState.BUDGET_EXHAUSTED, ExecutionState.TIMED_OUT},
    ExecutionState.TESTING: {ExecutionState.VERIFYING, ExecutionState.REPAIRING, ExecutionState.FAILED, ExecutionState.CANCELLED, ExecutionState.BUDGET_EXHAUSTED, ExecutionState.TIMED_OUT},
    ExecutionState.REPAIRING: {ExecutionState.IMPLEMENTING, ExecutionState.TESTING, ExecutionState.FAILED, ExecutionState.CANCELLED, ExecutionState.BUDGET_EXHAUSTED, ExecutionState.TIMED_OUT},
    ExecutionState.VERIFYING: {ExecutionState.COMPLETED, ExecutionState.REPAIRING, ExecutionState.FAILED, ExecutionState.CANCELLED, ExecutionState.TIMED_OUT},
    ExecutionState.COMPLETED: set(),
    ExecutionState.FAILED: set(),
    ExecutionState.CANCELLED: set(),
    ExecutionState.TIMED_OUT: set(),
    ExecutionState.BUDGET_EXHAUSTED: set(),
    ExecutionState.STALE: set(),
}


@dataclass
class PhaseBudget:
    max_provider_calls: int = 5
    max_input_tokens: int = 50_000
    max_output_tokens: int = 4_096
    max_tool_calls: int = 10
    max_files_read: int = 15
    max_commands: int = 5
    max_duration_seconds: float = 300.0
    max_brain_calls: int = 2


@dataclass
class PhaseMetrics:
    provider_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0
    files_read: int = 0
    commands_run: int = 0
    duration_seconds: float = 0.0
    retries: int = 0
    brain_calls: int = 0
    evidence_tokens: int = 0


@dataclass
class PhaseBudgetTracker:
    phase_budgets: Dict[str, PhaseBudget] = field(default_factory=dict)
    phase_metrics: Dict[str, PhaseMetrics] = field(default_factory=dict)

    def record_usage(self, phase_name: str, input_tokens: int = 0, output_tokens: int = 0, tool_calls: int = 1, files_read: int = 0, brain_calls: int = 0, duration_seconds: float = 0.0):
        if phase_name not in self.phase_metrics:
            self.phase_metrics[phase_name] = PhaseMetrics()
        m = self.phase_metrics[phase_name]
        m.input_tokens += input_tokens
        m.output_tokens += output_tokens
        m.tool_calls += tool_calls
        m.files_read += files_read
        m.brain_calls += brain_calls
        m.duration_seconds += duration_seconds
        m.provider_calls += 1

    def is_exhausted(self, phase_name: str) -> tuple[bool, str]:
        if phase_name not in self.phase_budgets or phase_name not in self.phase_metrics:
            return False, ""
        b = self.phase_budgets[phase_name]
        m = self.phase_metrics[phase_name]
        if m.provider_calls >= b.max_provider_calls:
            return True, f"Max provider calls ({b.max_provider_calls}) exceeded for phase '{phase_name}'"
        if m.input_tokens >= b.max_input_tokens:
            return True, f"Max input tokens ({b.max_input_tokens}) exceeded for phase '{phase_name}'"
        if m.duration_seconds >= b.max_duration_seconds:
            return True, f"Max duration ({b.max_duration_seconds}s) exceeded for phase '{phase_name}'"
        return False, ""


@dataclass
class ExecutionContract:
    task_intent: str
    success_criteria: List[str]
    prohibited_outcomes: List[str]
    repository_scope: List[str]
    allowed_file_patterns: List[str] = field(default_factory=lambda: ["*"])
    max_changed_files: int = 5
    max_changed_lines: int = 250
    required_arch_constraints: List[str] = field(default_factory=list)
    required_security_constraints: List[str] = field(default_factory=list)
    required_tests: List[str] = field(default_factory=list)
    optional_tests: List[str] = field(default_factory=list)
    max_provider_turns: int = 15
    investigation_budget: PhaseBudget = field(default_factory=lambda: PhaseBudget(max_provider_calls=5, max_files_read=15))
    planning_budget: PhaseBudget = field(default_factory=lambda: PhaseBudget(max_provider_calls=3))
    implementation_budget: PhaseBudget = field(default_factory=lambda: PhaseBudget(max_provider_calls=5))
    repair_budget: PhaseBudget = field(default_factory=lambda: PhaseBudget(max_provider_calls=4))
    verification_budget: PhaseBudget = field(default_factory=lambda: PhaseBudget(max_provider_calls=3))
    max_failed_test_cycles: int = 3
    max_patch_attempts: int = 3
    timeout_seconds: float = 600.0
    rollback_policy: str = "rollback_on_failed_verification"
    stop_conditions: List[str] = field(default_factory=lambda: ["verification_passed", "max_repairs_exhausted", "scope_violation"])


@dataclass
class ExecutionSession:
    execution_id: str
    repository_id: str
    repository_revision: str
    task_fingerprint: str
    task_category: str
    selected_brain_route: str
    evidence_pack_id: str
    model_provider: str
    exact_model_identifier: str
    contract: ExecutionContract
    state: ExecutionState = ExecutionState.RECEIVED
    tool_schema_version: str = "v0.5.2"
    execution_policy_version: str = "v0.5.2"
    created_at_utc: str = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())
    semantic_fingerprint: str = ""
    history_states: List[tuple[str, str]] = field(default_factory=list)

    def transition_to(self, target_state: ExecutionState, reason: str = "") -> bool:
        if self.state in TERMINAL_STATES:
            raise ValueError(f"Cannot transition from terminal state {self.state} to {target_state}")
        valid_targets = VALID_TRANSITIONS.get(self.state, set())
        if target_state not in valid_targets:
            raise ValueError(f"Invalid transition from {self.state} to {target_state}. Allowed: {valid_targets}")
        self.history_states.append((self.state.value, datetime.datetime.now(datetime.timezone.utc).isoformat()))
        self.state = target_state
        return True
