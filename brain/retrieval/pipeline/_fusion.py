"""Dense-primary fusion policy and v2 fused scores."""
from __future__ import annotations

from typing import Dict, List
from brain.config.settings import settings
from brain.retrieval.fusion import FusionPolicy, _v2_fused_scores as score_v2_channels
from brain.retrieval.types import ChannelCandidate, QueryRoute
from ._channels import _channel_weight


_LIN_CHANNEL_WEIGHTS_ATTRS: Dict[str, str] = {
    "vector": "RETRIEVAL_V2_LIN_W_VEC",
    "lexical": "RETRIEVAL_V2_LIN_W_LEX",
    "symbol": "RETRIEVAL_V2_LIN_W_SYM",
    "hints": "RETRIEVAL_V2_LIN_W_HINT",
    "graph": "RETRIEVAL_V2_LIN_W_GRAPH",
    "bm25": "RETRIEVAL_V2_LIN_W_BM25",
    "memory": "RETRIEVAL_V2_LIN_W_GRAPH",
    "card": "RETRIEVAL_V2_LIN_W_GRAPH",
}


def _fusion_policy(
    route: QueryRoute, *, v2_enabled: bool = False, v6_enabled: bool = False,
    expected_surface: str = "",
) -> FusionPolicy:
    return FusionPolicy(
        channel_weights={channel: _channel_weight(route, channel) for channel in _LIN_CHANNEL_WEIGHTS_ATTRS},
        linear_weights={channel: float(getattr(settings, attr, 0.25))
                        for channel, attr in _LIN_CHANNEL_WEIGHTS_ATTRS.items()},
        vector_multiplier=float(getattr(settings, "RETRIEVAL_V2_WRRF_VEC_WEIGHT", 2.0)),
        mode=getattr(settings, "RETRIEVAL_V2_FUSION", "wrrf"),
        v2_enabled=v2_enabled, v6_enabled=v6_enabled, expected_surface=expected_surface,
    )


def _v2_fused_scores(
    file_ranks: Dict[str, Dict[str, int]],
    candidates_by_channel: Dict[str, List[ChannelCandidate]],
    route: QueryRoute,
) -> Dict[str, float]:
    """Compatibility entry point; policy reads remain in the orchestrator."""
    return score_v2_channels(file_ranks, candidates_by_channel, _fusion_policy(route))
