"""Dense-primary locator fusion with bounded, non-additive side channels."""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from brain.search.filters import is_test_path, is_template_path

if TYPE_CHECKING:
    from brain.retrieval.service import RetrievalCandidate


def surface_weight(path: str, query: str) -> float:
    """Tests/templates remain first-class when the task explicitly asks for them."""
    if is_test_path(path) and not re.search(r"\b(?:tests?|testing|pytest|unittest|specs?|fixtures?|regressions?|coverage|test_\w+)\b", query, re.I):
        return 0.45
    if is_template_path(path) and not re.search(r"\b(?:templates?|html|styling|dashboard|branding)\b", query, re.I):
        return 0.4
    return 1.0


def linear_locator(candidates: list[RetrievalCandidate], query: str, budget: int) -> list[RetrievalCandidate]:
    """One maximum per file/channel; duplicate chunks/symbols cannot stack.

    Positive dense scores scale by their observed maximum (a singleton remains
    1.0). Noisy lexical/substrings contribute at most 0.5 combined. Exact named
    symbols can compete deliberately; no natural-language basename boost runs.
    """
    maxima = {name: max((max(0.0, candidate.channel_scores.get(name, 0.0)) for candidate in candidates), default=0.0)
              for name in ("vector", "lexical", "symbol")}
    weights = {"vector": 1.0, "lexical": 0.25, "symbol": 0.25}
    for candidate in candidates:
        candidate.score = sum(weights[name] * max(0.0, candidate.channel_scores.get(name, 0.0)) / maximum
                              for name, maximum in maxima.items() if maximum > 0)
        candidate.score += min(0.65, max(0.0, candidate.channel_scores.get("identifier", 0.0)) * 0.65)
        candidate.score *= surface_weight(candidate.path, query)
    return sorted(candidates, key=lambda candidate: (-candidate.score, candidate.path))[:budget]


def merge_linear(vector: list[RetrievalCandidate], symbols: list[RetrievalCandidate], query: str, budget: int):
    from brain.retrieval.service import RetrievalCandidate
    grouped: dict[str, RetrievalCandidate] = {}
    for item in [*vector, *symbols]:
        candidate = grouped.setdefault(item.path, RetrievalCandidate(path=item.path))
        candidate.symbols.extend(name for name in item.symbols if name not in candidate.symbols)
        for start, end in item.ranges:
            candidate.add_range(start, end)
        for channel, score in item.channel_scores.items():
            candidate.channel_scores[channel] = max(candidate.channel_scores.get(channel, 0.0), score)
    return linear_locator(list(grouped.values()), query, budget)
