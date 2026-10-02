"""Shared retrieval pipeline types."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List


class QueryRoute(str, Enum):
    EXACT_IDENTIFIER = "exact_identifier"
    CONCEPTUAL = "conceptual"
    DEPENDENCY = "dependency"
    DECISION_RULE = "decision_rule"
    CROSS_SURFACE = "cross_surface"
    GENERAL = "general"


@dataclass
class ChannelCandidate:
    channel: str
    item_id: str
    raw_score: float
    normalized_score: float = 0.0
    fusion_contribution: float = 0.0
    reranker_score: float = 0.0
    rank: int = 0
    included: bool = False
    exclusion_reason: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RetrievalTiming:
    query_parsing_ms: float = 0.0
    lexical_ms: float = 0.0
    symbol_ms: float = 0.0
    vector_ms: float = 0.0
    neo4j_ms: float = 0.0
    memory_ms: float = 0.0
    fusion_ms: float = 0.0
    rerank_ms: float = 0.0
    recall_pool_ms: float = 0.0
    late_interaction_ms: float = 0.0
    v5_rerank_ms: float = 0.0
    graph_expansion_ms: float = 0.0
    budget_selection_ms: float = 0.0
    critique_ms: float = 0.0
    llm_ms: float = 0.0
    rendering_ms: float = 0.0

    def to_dict(self) -> Dict[str, float]:
        return {k: round(v, 2) for k, v in self.__dict__.items()}


@dataclass
class SurfacePool:
    """Per-surface slice of the recall pool (v6 candidate generation)."""

    surface: str
    paths: List[str] = field(default_factory=list)
    quota: int = 0
    injected: List[str] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.paths)


@dataclass
class RecallStageResult:
    """Output of the v6 recall stage feeding deterministic rerank."""

    pool_paths: List[str] = field(default_factory=list)
    pool_limit: int = 0
    expected_surface: str = ""
    surface_pools: Dict[str, SurfacePool] = field(default_factory=dict)
    injected_paths: List[str] = field(default_factory=list)
    surface_counts: Dict[str, int] = field(default_factory=dict)

    def to_debug(self) -> Dict[str, Any]:
        return {
            "pool_size": len(self.pool_paths),
            "pool_limit": self.pool_limit,
            "expected_surface": self.expected_surface,
            "surface_counts": self.surface_counts,
            "injected": self.injected_paths,
        }


@dataclass
class RetrievalPipelineResult:
    route: QueryRoute
    vector_status: str
    candidates_by_channel: Dict[str, List[ChannelCandidate]] = field(default_factory=dict)
    fused_ranking: List[ChannelCandidate] = field(default_factory=list)
    reranked: List[ChannelCandidate] = field(default_factory=list)
    selected_paths: List[str] = field(default_factory=list)
    v5_top_paths: List[str] = field(default_factory=list)
    v5_rerank_debug: Dict[str, Any] = field(default_factory=dict)
    pool_paths: List[str] = field(default_factory=list)
    recall_debug: Dict[str, Any] = field(default_factory=dict)
    timing: RetrievalTiming = field(default_factory=RetrievalTiming)
    debug: Dict[str, Any] = field(default_factory=dict)
