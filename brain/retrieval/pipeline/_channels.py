"""Query routing and per-channel weight/strength helpers."""
from __future__ import annotations

import re
from typing import Dict, List, Tuple
from brain.config.settings import settings
from brain.database.models import File
from brain.retrieval.types import ChannelCandidate, QueryRoute, SurfacePool
from brain.search.filters import RRF_PIPELINE_WEIGHTS
from brain.search.lexical_ranking import score_path_for_keywords
from brain.search.surfaces import classify_surface, surfaces_for_pool


def classify_query_route(task_type: str, keywords: List[str], task_description: str) -> QueryRoute:
    lower = task_description.lower()
    if any(re.search(r"\b[A-Z][a-zA-Z0-9_]+\b", kw) for kw in keywords):
        return QueryRoute.EXACT_IDENTIFIER
    if any(token in lower for token in ("depends", "dependency", "import", "calls", "uses")):
        return QueryRoute.DEPENDENCY
    if any(token in lower for token in ("rule", "decision", "adr", "policy", "must not", "boundary", "enforce")):
        return QueryRoute.DECISION_RULE
    if any(
        token in lower for token in ("across", "frontend", "backend", "extension", "nginx", "docker", "deploy", "wire")
    ):
        return QueryRoute.CROSS_SURFACE
    if task_type in {"deletion_rename"}:
        return QueryRoute.EXACT_IDENTIFIER
    if task_type in {"feature", "refactor", "database", "analytics", "api_contract"}:
        return QueryRoute.CONCEPTUAL
    if task_type in {"bugfix", "design_change"} and len(keywords) >= 2:
        return QueryRoute.EXACT_IDENTIFIER
    return QueryRoute.GENERAL


def _channel_weight(route: QueryRoute, channel: str) -> float:
    if channel == "card":
        # Moderate card_vector weight (never card-first); route-neutral.
        return float(getattr(settings, "RETRIEVAL_CARD_VECTOR_WEIGHT", 0.9))
    base = RRF_PIPELINE_WEIGHTS.get(channel, 1.0)
    boosts = {
        QueryRoute.EXACT_IDENTIFIER: {"symbol": 2.0, "lexical": 1.6, "hints": 1.5, "vector": 0.55},
        QueryRoute.CONCEPTUAL: {"vector": 1.5, "lexical": 0.95, "symbol": 1.1},
        QueryRoute.DEPENDENCY: {"graph": 1.5, "symbol": 1.4},
        QueryRoute.DECISION_RULE: {"memory": 1.6, "lexical": 1.3, "hints": 1.2},
        QueryRoute.CROSS_SURFACE: {"graph": 1.2, "hints": 1.35, "symbol": 1.15, "vector": 0.85},
    }
    return base * boosts.get(route, {}).get(channel, 1.0)


def _path_keyword_hits(path: str, keywords: List[str], task_description: str = "") -> float:
    return score_path_for_keywords(path, keywords, task_description)


def _channel_strength(
    paths: List[str],
    limit: int,
    candidates: List[ChannelCandidate],
) -> float:
    if not paths:
        return 0.0
    by_path = {c.item_id: c for c in candidates}
    score = 0.0
    for idx, path in enumerate(paths[:limit]):
        cand = by_path.get(path)
        base = cand.normalized_score if cand else 0.5
        score += base / (idx + 1)
    return score


def select_surface_balanced_pool(
    reranked: List[ChannelCandidate],
    pool_limit: int,
    expected_surface: str,
    min_quota: int,
) -> Tuple[List[ChannelCandidate], Dict[str, SurfacePool]]:
    """v6 recall: guarantee each routed surface a quota before global score fill.

    Prevents a dominant surface (scripts/tests on engine bugfixes) from crowding
    tail required files out of the top-100 pool. Pure / DB-free.
    """
    by_surface: Dict[str, List[ChannelCandidate]] = {}
    for cand in reranked:
        by_surface.setdefault(classify_surface(cand.item_id), []).append(cand)

    selected: List[ChannelCandidate] = []
    seen: set[str] = set()
    pools: Dict[str, SurfacePool] = {}

    for surface in surfaces_for_pool(expected_surface):
        bucket = by_surface.get(surface, [])
        pool = SurfacePool(surface=surface, quota=min_quota)
        for cand in bucket[:min_quota]:
            if cand.item_id not in seen:
                selected.append(cand)
                seen.add(cand.item_id)
                pool.paths.append(cand.item_id)
        pools[surface] = pool

    # Global fill by reranker score until pool_limit reached.
    for cand in reranked:
        if len(selected) >= pool_limit:
            break
        if cand.item_id not in seen:
            selected.append(cand)
            seen.add(cand.item_id)

    return selected[:pool_limit], pools


def _synth_pool_candidate(path: str, floor_score: float, source: str) -> ChannelCandidate:
    """Synthetic recall-pool candidate for a co-rank injected path."""
    return ChannelCandidate(
        channel="fused",
        item_id=path,
        raw_score=floor_score,
        normalized_score=floor_score,
        reranker_score=floor_score,
        metadata={"ranks": {}, "injected": source},
    )


def _should_abstain(
    *,
    selected: list[str],
    reranked: list[ChannelCandidate],
    candidates_by_channel: dict[str, list[ChannelCandidate]],
    task_type: str,
    keywords: list[str] | None = None,
) -> bool:
    """Return true when retrieval has no grounded evidence for a locator pack.

    Dense similarity always returns a nearest neighbour, even for an unrelated
    query.  An empty result is safer than presenting that neighbour as context
    when every lexical/symbol/path/graph channel is empty and the top score is
    below a conservative heuristic confidence floor. Explicitly routed tasks retain the
    historical fail-open behavior because their surface constraints provide
    independent evidence.
    """
    v2_enabled = bool(settings.RETRIEVAL_V2_ENABLED)
    if not selected or task_type in {"deletion_rename", "database", "api_contract", "bugfix"}:
        return False
    if not reranked:
        return False
    grounded = any(candidates_by_channel.get(name) for name in ("symbol", "hints", "graph", "memory"))
    if grounded:
        return False
    if v2_enabled and float(reranked[0].reranker_score) >= 0.5:
        return False
    lexical = candidates_by_channel.get("lexical", [])
    if lexical:
        # Generic words such as app/configure/deployment create incidental
        # lexical hits.  Require at least one distinctive query term to occur
        # in a candidate path before treating that channel as evidence.
        distinctive = {token.lower() for token in (keywords or []) if len(token) >= 7}
        path_text = " ".join(candidate.item_id.lower() for candidate in lexical)
        summary_text = " ".join(
            str(candidate.metadata.get("summary", "")).lower() for candidate in lexical
        )
        if distinctive and any(token in f"{path_text} {summary_text}" for token in distinctive):
            return False
        if not distinctive:
            return False
        # Legacy scores are RRF-scaled, so use query/path evidence rather than
        # a numeric threshold. Two or more distinctive terms absent from every
        # lexical path indicate an unsupported domain (generic words created
        # the lexical hit), while one-term semantic queries remain fail-open.
        if not v2_enabled and len(distinctive) >= 2:
            return True
        return v2_enabled
    return v2_enabled and float(reranked[0].reranker_score) < 0.15


def _lexical_file_candidate(file_obj: File, rank: int) -> ChannelCandidate:
    """Build the lexical candidate with the file metadata used by abstention."""
    return ChannelCandidate("lexical", file_obj.path, 1.0 / rank, rank=rank,
                            metadata={"summary": file_obj.summary or ""})
