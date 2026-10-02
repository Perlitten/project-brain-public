"""Run and Event Data Models for Utility Benchmark Pilot.

Strict schemas for run configuration, event stream, Brain telemetry,
gold standard manifests, and evaluator outputs.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List


class ExecutionMode(str, Enum):
    CONTROL = "control"
    TREATMENT = "treatment"
    TREATMENT_OPTIONAL = "treatment_optional"
    TREATMENT_CONDITIONED = "treatment_conditioned"
    TREATMENT_AUTOMATIC_ROUTED = "treatment_automatic_routed"
    TREATMENT_ORACLE = "treatment_oracle"
    TREATMENT_PHASED_LOOP = "treatment_phased_loop"
    TREATMENT_PHASED_BRAIN = "treatment_phased_brain"


class TaskCategory(str, Enum):
    BUG_LOCALIZATION = "bug_localization"
    MULTI_FILE_CHANGE = "multi_file_change"
    ARCH_BOUNDARY = "arch_boundary"
    CROSS_REPO_CONTRACT = "cross_repo_contract"
    REGRESSION_REMEDIATION = "regression_remediation"
    CALIBRATION = "calibration"


class EventType(str, Enum):
    RUN_STARTED = "run_started"
    TOOL_CALLED = "tool_called"
    FILE_READ = "file_read"
    SEARCH_EXECUTED = "search_executed"
    BRAIN_QUERY_EXECUTED = "brain_query_executed"
    TEST_EXECUTED = "test_executed"
    FILE_MODIFIED = "file_modified"
    RUN_COMPLETED = "run_completed"


class PilotConclusion(str, Enum):
    METHODOLOGY_INVALID = "methodology_invalid"
    METHODOLOGY_NEEDS_REVISION = "methodology_needs_revision"
    BRAIN_HARMFUL_SIGNAL = "Brain_harmful_signal"
    BRAIN_NO_CLEAR_SIGNAL = "Brain_no_clear_signal"
    BRAIN_PROMISING_SIGNAL = "Brain_promising_signal"
    BRAIN_STRONG_PILOT_SIGNAL = "Brain_strong_pilot_signal"


@dataclass
class ModelConfig:
    provider: str = "Google DeepMind"
    model_identifier: str = "antigravity-gemini-3.6"
    temperature: float = 0.0
    max_tokens: int = 4096
    system_prompt_hash: str = ""
    task_prompt_hash: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AgentEvent:
    event_id: str
    run_id: str
    event_type: str
    timestamp_utc: str
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class BrainCallRecord:
    call_id: str
    endpoint_or_tool: str
    query: str
    result_ids: List[str] = field(default_factory=list)
    bytes_returned: int = 0
    tokens_returned: int = 0
    latency_ms: float = 0.0
    used_by_agent: bool = True
    context_type: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class GoldManifest:
    task_id: str
    task_name: str
    category: str
    gold_review_status: str  # 'two_person_reviewed' or 'single_reviewer'
    task_intent: str
    required_behavior: str
    acceptable_solution_families: List[str] = field(default_factory=list)
    prohibited_outcomes: List[str] = field(default_factory=list)
    relevant_repositories: List[str] = field(default_factory=list)
    required_files: List[str] = field(default_factory=list)
    useful_files: List[str] = field(default_factory=list)
    required_dependencies: List[str] = field(default_factory=list)
    expected_impact_set: List[str] = field(default_factory=list)
    mandatory_tests: List[str] = field(default_factory=list)
    arch_constraints: List[str] = field(default_factory=list)
    partial_credit_rules: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class EvaluatorResult:
    evaluator_name: str
    passed: bool
    score: float  # 0.0 to 100.0
    details: Dict[str, Any] = field(default_factory=dict)
    violations: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RunRecord:
    run_id: str
    benchmark_version: str
    task_id: str
    mode: str  # 'control' or 'treatment'
    randomization_order: int
    model_config: Dict[str, Any] = field(default_factory=dict)
    repository_id: str = "project-brain"
    repository_commit: str = ""
    brain_revision: str = "v0.5.0-rc1"
    environment_fingerprint: str = ""
    start_time_utc: str = ""
    end_time_utc: str = ""
    wall_clock_duration_s: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    tool_calls_count: int = 0
    files_read: List[str] = field(default_factory=list)
    files_changed: List[str] = field(default_factory=list)
    commands_run: List[str] = field(default_factory=list)
    brain_calls: List[Dict[str, Any]] = field(default_factory=list)
    patch_diff: str = ""
    test_results: Dict[str, Any] = field(default_factory=dict)
    evaluator_results: Dict[str, Any] = field(default_factory=dict)
    task_success: bool = False
    task_score: float = 0.0
    exit_status: str = "completed"  # 'completed', 'timed_out', 'error'
    anomalies: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> RunRecord:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
