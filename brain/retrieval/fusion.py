"""In-memory fusion stages, independent of database and provider orchestration.

Channel normalization updates the request-owned candidate records as before.
Policy values are explicit inputs; no runtime settings, I/O or shared cache is
read here. Insertion order is retained to preserve stable score ties.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Mapping

from brain.database.models import File
from brain.retrieval.types import ChannelCandidate, QueryRoute
from brain.search.filters import (
    HISTORICAL_AUTHORITY_NOTE, KNOWLEDGE_STATUS_HISTORICAL,
    get_contextual_retrieval_weight, knowledge_status_from_summary,
    should_exclude_from_retrieval, surface_fusion_weight,
)
from brain.search.lexical_ranking import score_path_for_keywords
from brain.search.surfaces import classify_surface


@dataclass(frozen=True)
class FusionPolicy:
    channel_weights: Mapping[str, float]
    linear_weights: Mapping[str, float]
    vector_multiplier: float
    mode: str
    v2_enabled: bool = False
    v6_enabled: bool = False
    expected_surface: str = ""


@dataclass(frozen=True)
class PreparedFusion:
    file_ranks: dict[str, dict[str, int]]
    channels: tuple[str, ...]


def _normalize_channel(scores: List[Tuple[str, float]]) -> Dict[str, float]:
    if not scores:
        return {}
    max_s = max(s for _, s in scores) or 1.0
    min_s = min(s for _, s in scores)
    span = max_s - min_s or 1.0
    return {item_id: (score - min_s) / span for item_id, score in scores}


def _content_match_bonus(path: str, summary: Optional[str], keywords: List[str]) -> float:
    if not summary:
        return 0.0
    lower_summary = summary.lower()
    hits = sum(1 for kw in keywords if len(kw) >= 3 and kw in lower_summary)
    # Reduced from 0.04/0.2 to 0.002/0.008 so summary cards don't overpower RRF candidate scores.
    return min(hits * 0.002, 0.008)


def _rank_paths_from_channel(candidates: List[ChannelCandidate]) -> List[str]:
    ranked = sorted(candidates, key=lambda c: (-c.normalized_score, c.rank))
    ordered: List[str] = []
    seen: set[str] = set()  # O(1) membership instead of scanning the list
    for cand in ranked:
        if cand.item_id in seen or should_exclude_from_retrieval(cand.item_id):
            continue
        seen.add(cand.item_id)
        ordered.append(cand.item_id)
    return ordered


def _is_strong_channel(candidates: List[ChannelCandidate]) -> bool:
    if not candidates:
        return False
    top = max(c.normalized_score for c in candidates)
    return top >= 0.2 or any(c.rank <= 3 for c in candidates)


def _v2_fused_scores(
    file_ranks: Dict[str, Dict[str, int]],
    candidates_by_channel: Dict[str, List[ChannelCandidate]],
    policy: FusionPolicy,
) -> Dict[str, float]:
    """Dense-primary fusion. Channels only contribute when they fired for that
    path — empty channels neither dilute nor boost."""
    mode = policy.mode
    out: Dict[str, float] = {}
    norm_by_channel: Dict[str, Dict[str, float]] = {}
    if mode == "linear":
        for ch, cands in candidates_by_channel.items():
            norm_by_channel[ch] = {c.item_id: c.normalized_score for c in cands}
    k_rrf = 60.0
    for path, ranks in file_ranks.items():
        score = 0.0
        for channel, rank in ranks.items():
            if mode == "linear":
                w = policy.linear_weights.get(channel, 0.25)
                score += w * norm_by_channel.get(channel, {}).get(path, 0.0)
            elif mode == "wrrf":
                w = policy.channel_weights.get(channel, 1.0)
                if channel == "vector":
                    w *= policy.vector_multiplier
                score += w / (k_rrf + rank)
            else:  # rrf — legacy formula
                w = policy.channel_weights.get(channel, 1.0)
                score += w / (k_rrf + rank)
        out[path] = score
    return out


def prepare_channels(
    candidates_by_channel: dict[str, list[ChannelCandidate]],
    route: QueryRoute,
    *,
    card_recall: bool,
) -> PreparedFusion:
    """Normalize request-owned records and expose exactly the file ranks to hydrate."""
    for channel, cands in candidates_by_channel.items():
        norm = _normalize_channel([(c.item_id, c.raw_score) for c in cands])
        for c in cands:
            c.normalized_score = norm.get(c.item_id, 0.0)

    # RRF fusion with route-aware channel weights
    file_ranks: Dict[str, Dict[str, int]] = {}
    fusion_channels: tuple[str, ...] = ("lexical", "symbol", "hints", "vector", "graph")
    if route == QueryRoute.DECISION_RULE:
        fusion_channels = fusion_channels + ("memory",)
    if card_recall and candidates_by_channel["card"]:
        fusion_channels = fusion_channels + ("card",)
    if candidates_by_channel.get("bm25"):
        fusion_channels = fusion_channels + ("bm25",)
    for channel, cands in candidates_by_channel.items():
        if channel not in fusion_channels:
            continue
        if channel in {"graph", "memory"} and not _is_strong_channel(cands):
            continue
        for c in cands:
            file_ranks.setdefault(c.item_id, {})[channel] = c.rank

    return PreparedFusion(file_ranks, fusion_channels)


def fuse_candidates(
    prepared: PreparedFusion,
    candidates_by_channel: dict[str, list[ChannelCandidate]],
    path_to_file: Mapping[str, File],
    *,
    policy: FusionPolicy,
    task_type: str,
    task_description: str,
    keywords_lower: list[str],
) -> list[ChannelCandidate]:
    """Apply the existing legacy/v2 formulas to hydrated file metadata."""
    file_ranks = prepared.file_ranks
    k_rrf = 60.0
    fused_scores: dict[str, ChannelCandidate] = {}
    v2_scores = (
        _v2_fused_scores(file_ranks, candidates_by_channel, policy)
        if policy.v2_enabled
        else {}
    )
    for path, ranks in file_ranks.items():
        if should_exclude_from_retrieval(path):
            continue
        file_rec = path_to_file.get(path)
        summary = file_rec.summary if file_rec else None
        if policy.v2_enabled:
            score = v2_scores.get(path, 0.0)
        else:
            score = 0.0
            for channel, rank in ranks.items():
                w = policy.channel_weights.get(channel, 1.0)
                contrib = w / (k_rrf + rank)
                score += contrib
            score *= get_contextual_retrieval_weight(
                path,
                file_rec.file_type if file_rec else None,
                task_type,
                task_description,
            )
            score += _content_match_bonus(path, summary, keywords_lower)
            score += score_path_for_keywords(path, keywords_lower, task_description) * 0.005
            if policy.v6_enabled and policy.expected_surface:
                score *= surface_fusion_weight(classify_surface(path), policy.expected_surface)
        fused = ChannelCandidate(
            channel="fused",
            item_id=path,
            raw_score=score,
            normalized_score=score,
            fusion_contribution=score,
            metadata={
                "ranks": ranks,
                "knowledge_status": knowledge_status_from_summary(summary),
            },
        )
        if fused.metadata["knowledge_status"] == KNOWLEDGE_STATUS_HISTORICAL:
            fused.metadata["authority_note"] = HISTORICAL_AUTHORITY_NOTE
        fused_scores[path] = fused

    fused_ranking = sorted(fused_scores.values(), key=lambda c: c.normalized_score, reverse=True)
    return fused_ranking


def rank_channel_paths(
    prepared: PreparedFusion, candidates_by_channel: dict[str, list[ChannelCandidate]],
) -> dict[str, list[str]]:
    channel_paths: Dict[str, List[str]] = {}
    for channel in prepared.channels:
        cands = candidates_by_channel.get(channel, [])
        if not cands:
            continue
        if channel in {"graph", "memory"} and not _is_strong_channel(cands):
            continue
        channel_paths[channel] = _rank_paths_from_channel(cands)

    return channel_paths
