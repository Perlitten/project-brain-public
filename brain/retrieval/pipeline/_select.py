"""Final path selection and qualified path-hint pinning."""
from __future__ import annotations

from brain.retrieval.types import ChannelCandidate
from brain.search.filters import enforce_precision_top_k, trim_ranked_files
from brain.search.path_hints import path_hint_bonus
from ._channels import _path_keyword_hits


def _pin_qualified_path_hints(
    candidates: list[ChannelCandidate], hints: list[str], pinned: list[str], limit: int,
) -> list[str]:
    """Retain indexed qualified file hints during keyword-based promotion.

    Basename/directory hints remain ranking signals. Only the existing strongest
    qualified-file match becomes a protected choice inside the output budget.
    """
    anchors = [candidate.item_id for candidate in sorted(
        candidates, key=lambda c: (-path_hint_bonus(c.item_id, hints), c.rank, c.item_id),
    ) if path_hint_bonus(candidate.item_id, hints) >= 0.55]
    return list(dict.fromkeys(anchors + pinned))[:limit]


def _select_final_paths(
    *,
    reranked: list[ChannelCandidate],
    v5_top: list[str],
    file_limit: int,
    precision_k: int,
    candidates_by_channel: dict[str, list[ChannelCandidate]],
    keywords_lower: list[str],
    task_type: str,
    task_description: str,
    pinned: list[str],
) -> list[str]:
    scored_tuples = [
        (candidate.reranker_score, candidate.item_id, {"final_score": candidate.reranker_score})
        for candidate in reranked
    ]
    trimmed = trim_ranked_files(
        scored_tuples,
        file_limit,
        min_keep=min(10, file_limit),
        aggressive=file_limit < 15,
    )
    tail_selected = [path for _, path, _ in trimmed]

    promoted: list[str] = []
    seen_selected: set[str] = set()
    for path in v5_top + tail_selected:
        if path not in seen_selected:
            promoted.append(path)
            seen_selected.add(path)
    for channel in ("hints", "lexical", "symbol"):
        for candidate in candidates_by_channel.get(channel, [])[:8]:
            if candidate.item_id in seen_selected:
                continue
            lexical_score = _path_keyword_hits(
                candidate.item_id,
                keywords_lower,
                task_description,
            )
            if lexical_score < 3.5 and channel != "hints":
                continue
            promoted.append(candidate.item_id)
            seen_selected.add(candidate.item_id)
    score_by_path = {candidate.item_id: candidate.reranker_score for candidate in reranked}
    promoted.sort(
        key=lambda path: score_by_path.get(path, 0.0)
        + _path_keyword_hits(path, keywords_lower, task_description) * 0.05,
        reverse=True,
    )
    pinned_set = set(pinned)
    rest = [path for path in promoted if path not in pinned_set]
    selected = (pinned + rest)[:file_limit]
    head = [path for path in v5_top if path in selected]
    selected = head + [path for path in selected if path not in head]
    selected = enforce_precision_top_k(
        selected,
        task_type,
        task_description,
        k=precision_k,
        priority_paths=v5_top + pinned,
    )[:file_limit]
    head = [path for path in v5_top if path in selected]
    return head + [path for path in selected if path not in head]
