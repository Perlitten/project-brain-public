"""Graphify v2 Schema, Node, and Relationship Definitions."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict


class NodeTypeV2(str, Enum):
    REPOSITORY = "Repository"
    REVISION = "Revision"
    GRAPH_GENERATION = "GraphGeneration"
    FILE = "File"
    MODULE = "Module"
    PACKAGE = "Package"
    SYMBOL = "Symbol"
    CLASS = "Class"
    FUNCTION = "Function"
    METHOD = "Method"
    CONFIGURATION = "Configuration"
    DATABASE_ENTITY = "DatabaseEntity"
    API_ENDPOINT = "APIEndpoint"
    WORKER_TASK = "WorkerTask"
    ARCHITECTURE_RULE = "ArchitectureRule"
    SUBSYSTEM = "Subsystem"


class RelationshipTypeV2(str, Enum):
    CONTAINS = "CONTAINS"
    DECLARES = "DECLARES"
    IMPORTS = "IMPORTS"
    CALLS = "CALLS"
    INHERITS = "INHERITS"
    IMPLEMENTS = "IMPLEMENTS"
    READS = "READS"
    WRITES = "WRITES"
    EXPOSES = "EXPOSES"
    CONFIGURES = "CONFIGURES"
    DEPENDS_ON = "DEPENDS_ON"
    BELONGS_TO_SUBSYSTEM = "BELONGS_TO_SUBSYSTEM"
    VIOLATES_RULE = "VIOLATES_RULE"
    IMPACTS = "IMPACTS"


@dataclass
class GraphNodeV2:
    node_type: NodeTypeV2
    qualified_id: str
    repository_id: str
    generation_id: str
    normalized_path: str = ""
    language: str = "python"
    properties: Dict[str, Any] = field(default_factory=dict)
    source_evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["node_type"] = self.node_type.value
        return d


@dataclass
class GraphRelationshipV2:
    rel_type: RelationshipTypeV2
    source_id: str
    target_id: str
    provenance: str
    extractor: str
    confidence: str = "exact"  # 'exact' or 'inferred'
    source_location: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["rel_type"] = self.rel_type.value
        return d
