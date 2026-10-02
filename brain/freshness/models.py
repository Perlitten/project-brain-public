"""Incremental Intelligence and Freshness Models — Phases C1, C3, C4."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class FreshnessState(str, Enum):
    CURRENT = "current"
    STALE = "stale"
    BUILDING = "building"
    FAILED = "failed"
    UNKNOWN = "unknown"
    SUPERSEDED = "superseded"


class ArtifactType(str, Enum):
    """Phase C3 — artifact kinds that carry an explicit freshness state."""

    REPOSITORY_REGISTRATION = "repository_registration"
    SOURCE_REVISION = "source_revision"
    REPOSITORY_GRAPH = "repository_graph"
    PORTFOLIO_GRAPH = "portfolio_graph"
    BASELINE = "baseline"
    TREND = "trend"
    IMPACT_ANALYSIS = "impact_analysis"
    REMEDIATION_PLAN = "remediation_plan"
    PATCH_PROPOSAL = "patch_proposal"
    EXPERIMENT_RESULT = "experiment_result"


class InvalidationEventType(str, Enum):
    REPOSITORY_REVISION_CHANGED = "repository_revision_changed"
    GRAPH_GENERATION_ACTIVATED = "graph_generation_activated"
    POLICY_CHANGED = "policy_changed"
    SUBSYSTEM_CONFIG_CHANGED = "subsystem_configuration_changed"
    PORTFOLIO_CONTRACT_CHANGED = "portfolio_contract_changed"
    WAIVER_CHANGED = "waiver_changed"
    REMEDIATION_STRATEGY_CHANGED = "remediation_strategy_changed"
    TEST_MAPPING_CHANGED = "test_mapping_changed"


class RebuildDecision(str, Enum):
    """Phase C2 — how generation N+1 was (or must be) produced."""

    INCREMENTAL = "incremental"
    FULL_REBUILD = "full_rebuild"
    NO_CHANGE = "no_change"


@dataclass
class IncrementalPlan:
    """Phase C1 — Plan for incremental graph update.

    The plan is a pure description of what changed between two revisions and
    which graph entities that invalidates. It never mutates a generation.
    """

    base_revision: str
    candidate_revision: str
    repository_id: str = ""
    added_files: List[str] = field(default_factory=list)
    modified_files: List[str] = field(default_factory=list)
    deleted_files: List[str] = field(default_factory=list)
    renamed_files: List[Tuple[str, str]] = field(default_factory=list)  # [(old, new)]
    affected_symbols: List[str] = field(default_factory=list)
    affected_relationships: int = 0
    dependent_entities: List[str] = field(default_factory=list)
    stale_nodes: int = 0
    stale_relationships: int = 0
    re_extraction_scope: List[str] = field(default_factory=list)
    fallback_reason: Optional[str] = None
    decision: str = RebuildDecision.INCREMENTAL.value

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["renamed_files"] = [list(pair) for pair in self.renamed_files]
        d["total_changes"] = self.total_changes
        return d

    @property
    def total_changes(self) -> int:
        return (
            len(self.added_files)
            + len(self.modified_files)
            + len(self.deleted_files)
            + len(self.renamed_files)
        )

    @property
    def affected_paths(self) -> List[str]:
        """Every path whose graph entities must be dropped from generation N."""
        paths = set(self.added_files) | set(self.modified_files) | set(self.deleted_files)
        for old, new in self.renamed_files:
            paths.add(old)
            paths.add(new)
        return sorted(paths)

    def semantic_fingerprint(self) -> str:
        payload = json.dumps(
            {
                "base": self.base_revision,
                "candidate": self.candidate_revision,
                "repository": self.repository_id,
                "paths": self.affected_paths,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


@dataclass
class DerivationReport:
    """Phase C2 — result of deriving generation N+1 from generation N."""

    repository_id: str
    base_generation_id: str
    new_generation_id: str
    decision: str
    base_revision: str = ""
    candidate_revision: str = ""
    copied_nodes: int = 0
    copied_relationships: int = 0
    added_nodes: int = 0
    replaced_paths: int = 0
    tombstoned_nodes: int = 0
    dropped_relationships: int = 0
    renamed_paths: int = 0
    fallback_reason: str = ""
    activated: bool = False
    validation_errors: List[str] = field(default_factory=list)
    duration_seconds: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class FreshnessRecord:
    """Phase C3 — Freshness state for an artifact.

    ``state`` is only ever ``current`` when ``observed_revision`` was verified
    against the repository at record time; otherwise the tracker downgrades to
    ``unknown``.
    """

    artifact_id: str
    artifact_type: str
    repository_id: str
    state: str = FreshnessState.UNKNOWN.value
    source_revision: str = ""
    observed_revision: str = ""
    evidence: str = ""
    last_checked_utc: str = ""
    reason: str = ""
    superseded_by: str = ""
    version: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "FreshnessRecord":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class InvalidationEvent:
    """Phase C4 — Internal invalidation event."""

    event_id: str
    event_type: str
    artifact_id: str
    caused_by: str
    timestamp_utc: str
    repository_id: str = ""
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def create(
        cls,
        event_type: str,
        artifact_id: str,
        caused_by: str,
        repository_id: str = "",
        details: Optional[Dict] = None,
    ) -> "InvalidationEvent":
        return cls(
            event_id=f"inv-{uuid.uuid4().hex[:10]}",
            event_type=event_type,
            artifact_id=artifact_id,
            caused_by=caused_by,
            timestamp_utc=_utc_now(),
            repository_id=repository_id,
            details=details or {},
        )
