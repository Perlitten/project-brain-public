"""Bounded lexical support checks for runtime retrieval candidates.

This is deliberately a conservative companion to semantic retrieval.  It only
abstains when a query has several concrete Latin anchors and too few are supported
by the returned paths, symbols, or loaded slice content.  A miss is therefore
treated as weak evidence of an unsupported domain, never as proof that a
semantic query is irrelevant.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}")
_IDENTIFIER_PART = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|[0-9]+")
_STOPWORDS = frozenset(
    {
        "about", "after", "also", "and", "are", "can", "does", "each",
        "find", "for", "from", "get", "has", "how", "into", "that",
        "the", "this", "through", "use", "used", "what", "when", "where",
        "which", "with", "why", "you",
    }
)
_GENERIC = frozenset(
    {
        "add", "configure", "explain", "fix", "work",
        "application", "behavior", "behaviour", "configuration", "describe",
        "description", "feature", "function", "implement", "implementation",
        "module", "project", "provide", "request", "response", "service",
        "support", "system",
    }
)


def _terms(query: str) -> tuple[str, ...]:
    def canonical(token: str) -> str:
        token = token.casefold()
        if len(token) > 4 and token.endswith("s") and not token.endswith("ss"):
            return token[:-1]
        return token

    return tuple(
        dict.fromkeys(
            canonical(token)
            for token in _TOKEN.findall(query)
            if canonical(token) not in _STOPWORDS | _GENERIC
        )
    )


def _identifier_tokens(value: str) -> set[str]:
    """Split paths, snake_case and CamelCase without substring matches."""
    tokens: set[str] = set()
    for chunk in re.split(r"[^A-Za-z0-9]+", value):
        if not chunk:
            continue
        tokens.update(part.casefold() for part in _IDENTIFIER_PART.findall(chunk))
        tokens.add(chunk.casefold())
    return tokens


def _candidate_fields(candidate: Any) -> tuple[str, str, str]:
    path = getattr(candidate, "path", None) or getattr(candidate, "item_id", None) or ""
    symbols = getattr(candidate, "symbols", ()) or ()
    # Test fixtures can quote the user's question verbatim. They are useful
    # retrieval noise, but cannot establish that a production feature exists.
    if re.search(r"(?:^|/)(?:tests?|fixtures?)(?:/|$)", str(path), re.I):
        return "", "", ""
    return str(path), " ".join(str(symbol) for symbol in symbols), " ".join(
        [str(path), *(str(symbol) for symbol in symbols)]
    )


def _slice_text(slices: Iterable[Any]) -> str:
    parts: list[str] = []
    for item in slices:
        if isinstance(item, Mapping):
            path = str(item.get("path", ""))
            content = str(item.get("content", ""))
        else:
            path = str(getattr(item, "path", ""))
            content = str(getattr(item, "content", ""))
        if re.search(r"(?:^|/)(?:tests?|fixtures?)(?:/|$)", path, re.I):
            continue
        # Comments and quoted fixture prompts are incidental evidence. Keep
        # identifiers and prose from executable lines only.
        lines = []
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith(("'''", '\"\"\"')):
                continue
            lines.append(re.sub(r"(['\"])(?:\\.|(?!\1).)*\1", " ", line))
        parts.append(path)
        parts.append("\n".join(lines))
    return " ".join(parts).casefold()


def query_evidence_support(
    query: str,
    candidates: Sequence[Any],
    slices: Iterable[Any] = (),
) -> dict[str, Any]:
    """Return explainable bounded support signals for a runtime query.

    ``supported_terms`` is based on whole identifier tokens in candidate
    locator fields and bounded loaded slices.  Queries without at least two
    concrete Latin anchors are left undecided, preserving semantic and
    non-English queries.  Callers may abstain only when ``should_abstain`` is
    true; this function itself never changes retrieval state.
    """
    terms = _terms(query)
    fields = [_candidate_fields(candidate) for candidate in candidates]
    slice_content = _slice_text(slices)
    supported: list[str] = []
    authoritative: list[str] = []
    for term in terms:
        path_or_symbol = " ".join(path + " " + symbols for path, symbols, _ in fields)
        identifier_tokens = {
            canonical if not (len(canonical) > 4 and canonical.endswith("s") and not canonical.endswith("ss")) else canonical[:-1]
            for canonical in _identifier_tokens(path_or_symbol)
        }
        if term in identifier_tokens:
            supported.append(term)
            authoritative.append(term)
        elif term in {
            token[:-1] if len(token) > 4 and token.endswith("s") and not token.endswith("ss") else token
            for token in _identifier_tokens(slice_content)
        }:
            supported.append(term)
    missing = tuple(term for term in terms if term not in supported)
    decided = len(terms) >= 2
    # A single incidental path/symbol word must not rescue an unrelated
    # domain. Require support for a substantial share of concrete anchors;
    # exact named identifiers are already weighted by the token split above.
    enough_support = bool(supported) and len(supported) * 2 >= len(terms)
    return {
        "terms": terms,
        "supported_terms": tuple(supported),
        "missing_terms": missing,
        "decided": decided,
        "authoritative_terms": tuple(authoritative),
        "should_abstain": bool(decided and not enough_support and candidates),
    }


def should_abstain_for_unsupported_query(
    query: str,
    candidates: Sequence[Any],
    slices: Iterable[Any] = (),
) -> bool:
    """Conservative predicate suitable for runtime context fail-closed use."""
    return bool(query_evidence_support(query, candidates, slices)["should_abstain"])
