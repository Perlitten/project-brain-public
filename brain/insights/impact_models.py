"""Deep Change-Impact Analysis Request and Result Models."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List


@dataclass
class ImpactedEntity:
    entity_id: str
    normalized_path: str
    entity_type: str
    subsystem: str
    evidence_category: str  # 'direct', 'statically_referenced', 'graph_inferred', 'configuration_linked', 'test_associated'
    confidence: str  # 'confirmed', 'high', 'medium', 'low'
    confidence_reasons: List[str] = field(default_factory=list)
    traversal_path: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ImpactResult:
    run_id: str
    repository: str
    base_revision: str
    candidate_revision: str
    created_at_utc: str
    directly_changed_files: List[str]
    impacted_entities: List[ImpactedEntity]
    impacted_subsystems: List[str]
    impacted_api_endpoints: List[str]
    impacted_worker_tasks: List[str]
    suggested_tests: List[str]
    risk_reasons: List[str]
    truncated: bool = False

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["impacted_entities"] = [e.to_dict() for e in self.impacted_entities]
        return d
