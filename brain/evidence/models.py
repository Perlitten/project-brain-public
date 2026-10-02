"""Evidence Pack v2 Data Models.

Provides structured evidence item schemas with strict provenance, freshness,
validation status, and machine-consumable agent summaries.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class EvidenceSufficiencyEnum(str, Enum):
    SUFFICIENT = "sufficient"
    PARTIALLY_SUFFICIENT = "partially_sufficient"
    INSUFFICIENT = "insufficient"
    STALE = "stale"
    CONFLICTING = "conflicting"


@dataclass
class EvidenceItem:
    evidence_id: str
    repository: str
    source_revision: str
    file_path: str
    symbol: str
    line_range: str
    evidence_type: str  # 'verified_fact', 'dependency', 'architecture_constraint', 'test'
    channel_score: float
    final_score: float
    freshness: str  # 'current', 'stale'
    confidence: float
    why_included: str
    source_excerpt: str
    truncation_status: bool = False
    provenance: str = "ast_graph"
    validation_status: str = "valid"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AgentOrientedSummary:
    route: str
    sufficiency: str
    recommended_files: List[str] = field(default_factory=list)
    required_constraints: List[str] = field(default_factory=list)
    likely_tests: List[str] = field(default_factory=list)
    unresolved_questions: List[str] = field(default_factory=list)
    do_not_assume: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class EvidencePackV2:
    pack_id: str
    route: str
    sufficiency: EvidenceSufficiencyEnum
    verified_facts: List[EvidenceItem] = field(default_factory=list)
    likely_relevant_files: List[str] = field(default_factory=list)
    dependency_evidence: List[EvidenceItem] = field(default_factory=list)
    architecture_constraints: List[EvidenceItem] = field(default_factory=list)
    known_tests: List[str] = field(default_factory=list)
    configuration_evidence: List[EvidenceItem] = field(default_factory=list)
    uncertainty_notes: List[str] = field(default_factory=list)
    agent_summary: Optional[AgentOrientedSummary] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["sufficiency"] = self.sufficiency.value
        return d
