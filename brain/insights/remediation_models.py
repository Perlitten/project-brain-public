"""Assisted Remediation Plan Data Models."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class RemediationOption:
    option_id: str
    title: str
    description: str
    architectural_improvement: str  # 'low', 'medium', 'high'
    implementation_effort: str  # 'low', 'medium', 'high'
    blast_radius: str  # 'low', 'medium', 'high'
    operational_risk: str  # 'low', 'medium', 'high'
    pros: List[str] = field(default_factory=list)
    cons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RemediationPlan:
    plan_id: str
    schema_version: int
    repository: str
    source_revision: str
    finding_ids: List[str]
    graph_generation: str
    problem_statement: str
    evidence: Dict[str, Any]
    affected_files: List[str]
    affected_symbols: List[str]
    impacted_subsystems: List[str]
    remediation_options: List[RemediationOption]
    recommended_option_index: int
    implementation_steps: List[str]
    validation_steps: List[str]
    tests_to_run: List[str]
    rollback_plan: List[str]
    risks: List[str]
    human_approvals_required: List[str]
    state: str  # 'proposed', 'acknowledged', 'approved', 'rejected', 'superseded', 'implemented', 'verified', 'invalidated'
    created_at_utc: str
    updated_at_utc: str
    patch_diff: Optional[str] = None
    file_hashes: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
