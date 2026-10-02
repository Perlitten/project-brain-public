"""Remediation Experiment Models — Phases E1, E2, E4, E5, E8, E9.

An experiment compares several candidate remediation options against the same
base revision, each in its own disposable workspace, and produces a
deterministic recommendation.

A recommendation is not an approval. Project Brain does not apply patches to
authoritative repositories, commit changes, push branches, merge pull requests,
or deploy software.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = 1


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class ExperimentState(str, Enum):
    PROPOSED = "proposed"
    QUEUED = "queued"
    PREPARING = "preparing"
    RUNNING = "running"
    COMPARING = "comparing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    STALE = "stale"
    SUPERSEDED = "superseded"


#: States from which no further orchestration may start.
TERMINAL_STATES = frozenset(
    {
        ExperimentState.COMPLETED.value,
        ExperimentState.FAILED.value,
        ExperimentState.CANCELLED.value,
        ExperimentState.SUPERSEDED.value,
    }
)


class ExperimentConclusion(str, Enum):
    RECOMMEND_OPTION = "recommend_option"
    MULTIPLE_EQUIVALENT = "multiple_equivalent"
    NO_SAFE_OPTION = "no_safe_option"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    ALL_FAILED = "all_failed"
    EXPERIMENT_STALE = "experiment_stale"


class HumanAction(str, Enum):
    ACKNOWLEDGE = "acknowledge"
    ACCEPT_FOR_EXPORT = "accept_for_export"
    REJECT = "reject"
    REQUEST_ANOTHER = "request_another"
    MARK_APPLIED = "mark_applied"
    VERIFY_EXTERNAL = "verify_external"


#: Actions that require a completed experiment before they mean anything.
ACTIONS_REQUIRING_CONCLUSION = frozenset(
    {
        HumanAction.ACKNOWLEDGE.value,
        HumanAction.ACCEPT_FOR_EXPORT.value,
        HumanAction.REJECT.value,
    }
)

#: An option may only be exported once a human has explicitly accepted it.
#: Acceptance records a decision — it never applies anything.
ACTIONS_ALLOWING_EXPORT = frozenset({HumanAction.ACCEPT_FOR_EXPORT.value})


@dataclass
class HumanActionRecord:
    """One entry in the human workflow log — Phase E8.

    These are workflow records only. Accepting an option does not apply it.
    """

    action: str
    actor: str = "unknown"
    option_id: str = ""
    note: str = ""
    recorded_at_utc: str = field(default_factory=utc_now)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> HumanActionRecord:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class ExperimentOption:
    """A single remediation option within an experiment — Phase E2."""

    option_id: str
    source_remediation_option: str = ""
    candidate_patch: str = ""
    patch_provenance: str = ""
    expected_files: List[str] = field(default_factory=list)
    expected_effect: str = ""
    workspace_id: str = ""
    #: Empty means "inherit the experiment's profile" — an option that silently
    #: defaulted to a different profile would be compared on different evidence.
    validation_profile: str = ""

    # Measured outcome.
    executed: bool = False
    passed_required: bool = False
    passed_optional: bool = False
    architecture_improved: bool = False
    new_findings: int = 0
    new_critical_findings: int = 0
    resolved_findings: int = 0
    cycle_delta: int = 0
    forbidden_edge_delta: int = 0
    coupling_delta: float = 0.0
    hotspot_delta: float = 0.0
    impact_blast_radius: int = 0
    changed_files: int = 0
    changed_lines: int = 0
    rollback_complexity: int = 0
    unresolved_uncertainty: List[str] = field(default_factory=list)
    validation_duration: float = 0.0
    failure_reason: str = ""
    disqualified: bool = False
    disqualification_reason: str = ""
    prominent_warnings: List[str] = field(default_factory=list)
    artifacts: Dict[str, str] = field(default_factory=dict)
    results: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> ExperimentOption:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class Experiment:
    """A remediation experiment comparing multiple options — Phase E1."""

    experiment_id: str
    repository_id: str
    base_revision: str
    source_plan_id: str = ""
    source_finding_id: str = ""
    graph_generation: str = ""
    policy_version: str = ""
    creation_actor: str = "unknown"
    options: List[ExperimentOption] = field(default_factory=list)
    workspace_ids: List[str] = field(default_factory=list)
    validation_profile: str = "python-standard"
    max_parallel_options: int = 1
    option_timeout_seconds: int = 900
    policy_allows_new_critical: bool = False
    retain_workspaces: bool = False
    state: str = ExperimentState.PROPOSED.value
    conclusion: str = ""
    recommendation_note: str = (
        "A recommendation is not an approval. Project Brain does not apply "
        "patches to authoritative repositories."
    )
    recommended_option_id: str = ""
    failure_reason: str = ""
    cancel_requested: bool = False
    human_action: str = ""
    human_action_timestamp: str = ""
    human_actions: List[HumanActionRecord] = field(default_factory=list)
    created_at_utc: str = ""
    updated_at_utc: str = ""
    completed_at_utc: str = ""
    semantic_fingerprint: str = ""
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> Experiment:
        options = [ExperimentOption.from_dict(o) for o in d.get("options", [])]
        actions = [HumanActionRecord.from_dict(a) for a in d.get("human_actions", [])]
        known = {
            k: v
            for k, v in d.items()
            if k in cls.__dataclass_fields__ and k not in ("options", "human_actions")
        }
        result = cls(**known)
        result.options = options
        result.human_actions = actions
        return result

    def compute_fingerprint(self) -> str:
        """Identity of the *inputs* an experiment was reasoned about.

        Option patches are part of it: re-running with a different candidate is
        a different experiment, even against the same revision and plan.
        """
        parts = [
            self.repository_id,
            self.base_revision,
            self.source_plan_id,
            self.source_finding_id,
            self.graph_generation,
            self.policy_version,
        ]
        for opt in sorted(self.options, key=lambda o: o.option_id):
            parts.append(opt.option_id)
            parts.append(hashlib.sha256(opt.candidate_patch.encode("utf-8")).hexdigest())
        return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]

    def get_option(self, option_id: str) -> Optional[ExperimentOption]:
        return next((o for o in self.options if o.option_id == option_id), None)


@dataclass
class ComparisonResult:
    """Deterministic comparison of experiment options — Phase E4."""

    experiment_id: str
    dimensions: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    normalized: Dict[str, Dict[str, float]] = field(default_factory=dict)
    scores: Dict[str, float] = field(default_factory=dict)
    rankings: List[str] = field(default_factory=list)
    disqualified: List[Dict[str, str]] = field(default_factory=list)
    ties: List[List[str]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    conclusion: str = ""
    recommended_option_id: str = ""
    formula: str = ""
    comparator: str = "deterministic_weighted_sum"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> ComparisonResult:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class ExportStaleError(RuntimeError):
    """Raised when the authoritative revision no longer matches the base."""


class ExportNotAuthorizedError(PermissionError):
    """Raised when nobody has accepted the option being exported."""


@dataclass
class PatchExport:
    """Exported candidate patch package — Phase E9.

    Exporting produces a file for a human to review and apply themselves. It
    does not touch the authoritative repository.
    """

    export_id: str
    experiment_id: str
    option_id: str
    repository_id: str
    base_revision: str
    unified_diff: str
    expected_source_hashes: Dict[str, str] = field(default_factory=dict)
    validation_summary: Dict[str, Any] = field(default_factory=dict)
    architecture_comparison: Dict[str, Any] = field(default_factory=dict)
    tests_run: List[str] = field(default_factory=list)
    known_limitations: List[str] = field(default_factory=list)
    application_instructions: List[str] = field(default_factory=list)
    rollback_instructions: List[str] = field(default_factory=list)
    artifact_checksums: Dict[str, str] = field(default_factory=dict)
    authorized_by: str = ""
    revalidated: bool = False
    exported_at_utc: str = ""
    stale: bool = False
    stale_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> PatchExport:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def new_experiment_id() -> str:
    return f"exp-{uuid.uuid4().hex[:12]}"


def new_export_id() -> str:
    return f"pex-{uuid.uuid4().hex[:10]}"
