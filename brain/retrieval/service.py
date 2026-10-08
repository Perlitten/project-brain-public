"""One deadline-aware retrieval contract for all agent-facing fast paths."""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import or_, select

from brain.database.models import File, Symbol
from brain.database.repository_utils import get_repository_by_path
from brain.database.session import async_session_factory
from brain.memory.repo_freshness import assess_repository_freshness
from brain.search.code_search import search_code
from brain.search.symbol_ranking import symbol_relevance_order


RetrievalIntent = Literal["locator", "runtime_context", "ask", "impact", "deep"]
_IDENTIFIER_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b")


@dataclass
class RetrievalCandidate:
    path: str
    symbols: list[str] = field(default_factory=list)
    ranges: list[tuple[int, int]] = field(default_factory=list)
    channel_scores: dict[str, float] = field(default_factory=dict)
    score: float = 0.0

    def add_range(self, start: Any, end: Any) -> None:
        if isinstance(start, int) and isinstance(end, int) and start > 0 and end >= start:
            item = (start, end)
            if item not in self.ranges:
                self.ranges.append(item)

    def to_locator_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "symbols": sorted(set(self.symbols))[:8],
            "ranges": [list(item) for item in sorted(set(self.ranges))[:8]],
            "why": sorted(self.channel_scores),
            "score": round(self.score, 5),
        }


@dataclass
class RetrievalResult:
    query: str
    intent: RetrievalIntent
    repository: dict[str, Any]
    candidates: list[RetrievalCandidate]
    degraded: list[str] = field(default_factory=list)
    timings_ms: dict[str, float] = field(default_factory=dict)

    @property
    def freshness(self) -> str:
        freshness = self.repository.get("freshness") or {}
        return str(freshness.get("status") or ("unknown" if self.repository else "missing"))


class RetrievalService:
    """Adapt existing retrieval engines into one small, typed output boundary.

    The legacy ranking implementation remains available for shadow comparison,
    while every v2 agent surface consumes this result rather than rebuilding its
    own files/symbols/chunks interpretation.
    """

    async def retrieve(
        self,
        query: str,
        repo_path: str | Path | None,
        intent: RetrievalIntent = "locator",
        candidate_budget: int = 5,
        deadline_s: float = 5.0,
    ) -> RetrievalResult:
        if candidate_budget < 1:
            raise ValueError("candidate_budget must be positive")
        started = time.perf_counter()
        # The response budget is applied after merging channels. Keep the
        # supported ten-candidate window so small locators retain recall.
        recall_budget = max(candidate_budget, 10)
        repo_value = str(repo_path) if repo_path is not None else None
        repository: dict[str, Any] = {"requested_path": repo_value, "found": False}
        repo_record = None
        if repo_value:
            repo_record = await get_repository_by_path(repo_value)
            if repo_record is None:
                return RetrievalResult(
                    query=query,
                    intent=intent,
                    repository=repository,
                    candidates=[],
                    degraded=["repository_not_indexed"],
                    timings_ms={"total": round((time.perf_counter() - started) * 1000, 2)},
                )

        # Symbol route runs in parallel with vector search; results are merged
        # below. Early return only if vector search fails entirely.
        symbol_task = asyncio.create_task(
            self._known_symbol_route(query, repo_record, recall_budget)
        )

        degraded: list[str] = []
        try:
            raw = await asyncio.wait_for(
                search_code(query, limit=min(recall_budget, 10), repo_path=repo_value),
                timeout=max(0.05, deadline_s),
            )
        except asyncio.TimeoutError:
            # Fall back to symbol-only results if vector search times out
            fast_candidates = await symbol_task
            if fast_candidates:
                repository_fast: dict[str, Any] = {
                    "requested_path": repo_value,
                    "found": repo_record is not None,
                    "fast_route": "symbol",
                }
                if repo_record is not None:
                    repository_fast.update({
                        "repository_id": repo_record.id,
                        "repository_path": repo_record.path,
                        "indexed_revision": repo_record.last_indexed_commit,
                        "freshness": await assess_repository_freshness(repo_record),
                    })
                return RetrievalResult(
                    query=query,
                    intent=intent,
                    repository=repository_fast,
                    candidates=fast_candidates[:candidate_budget],
                    degraded=["deadline_exceeded"],
                    timings_ms={
                        "symbol": round((time.perf_counter() - started) * 1000, 2),
                        "total": round((time.perf_counter() - started) * 1000, 2),
                    },
                )
            return RetrievalResult(
                query=query,
                intent=intent,
                repository=repository,
                candidates=[],
                degraded=["deadline_exceeded"],
                timings_ms={"total": round((time.perf_counter() - started) * 1000, 2)},
            )
        except Exception as exc:  # Keep optional retrieval failure typed and fail-open for locators.
            fast_candidates = await symbol_task
            if fast_candidates:
                repository_fast = {
                    "requested_path": repo_value,
                    "found": repo_record is not None,
                    "fast_route": "symbol",
                }
                if repo_record is not None:
                    repository_fast.update({
                        "repository_id": repo_record.id,
                        "repository_path": repo_record.path,
                        "indexed_revision": repo_record.last_indexed_commit,
                        "freshness": await assess_repository_freshness(repo_record),
                    })
                return RetrievalResult(
                    query=query,
                    intent=intent,
                    repository=repository_fast,
                    candidates=fast_candidates[:candidate_budget],
                    degraded=[f"search_error:{type(exc).__name__}"],
                    timings_ms={"total": round((time.perf_counter() - started) * 1000, 2)},
                )
            return RetrievalResult(
                query=query,
                intent=intent,
                repository=repository,
                candidates=[],
                degraded=[f"search_error:{type(exc).__name__}"],
                timings_ms={"total": round((time.perf_counter() - started) * 1000, 2)},
            )

        # Merge vector search candidates with symbol route candidates.
        # Symbol matches boost files that contain explicitly named symbols,
        # but vector search finds files via semantic similarity even when
        # symbol names don't match query words.
        symbol_candidates = await symbol_task
        repository = dict(raw.get("repository_scope") or repository)
        if symbol_candidates:
            repository["fast_route"] = "symbol"
        vector_status = raw.get("vector_status")
        if vector_status is not None and str(vector_status).strip().casefold() != "ok":
            degraded.append(f"vector:{vector_status}")
        vector_candidates = self._normalize(raw, recall_budget)
        # Extract query identifiers for file-name bonus in merge.
        _STOP = {
            "the", "and", "for", "with", "from", "that", "this", "what", "why",
            "how", "when", "where", "did", "does", "was", "were", "are", "not",
            "but", "all", "any", "can", "has", "have", "had", "will", "would",
            "could", "should", "into", "than", "then", "them", "they", "their",
            "there", "here", "which", "who", "whom", "its", "our", "your", "you",
            "she", "him", "her", "his", "get", "set", "put", "run", "use", "try",
            "new", "old", "one", "two", "three", "job", "fail", "deploy",
            "brain", "project", "vector", "embedding", "revision", "comparison",
            "handle", "across", "explain", "request", "user", "trace", "flows",
            "flow", "down",
        }
        query_identifiers = [
            i for i in _IDENTIFIER_RE.findall(query)
            if len(i) >= 4 and i.lower() not in _STOP
        ][:12]
        candidates = self._merge_candidates(vector_candidates, symbol_candidates, candidate_budget, query_identifiers)
        return RetrievalResult(
            query=query,
            intent=intent,
            repository=repository,
            candidates=candidates,
            degraded=degraded,
            timings_ms={"total": round((time.perf_counter() - started) * 1000, 2)},
        )

    @staticmethod
    def _merge_candidates(
        vector: list[RetrievalCandidate],
        symbol: list[RetrievalCandidate],
        budget: int,
        query_identifiers: list[str] | None = None,
    ) -> list[RetrievalCandidate]:
        """Merge vector and symbol candidates, boosting files that appear in both."""
        merged: dict[str, RetrievalCandidate] = {}
        # Vector candidates get base score from semantic similarity
        for cand in vector:
            merged[cand.path] = cand
        # Symbol candidates boost existing entries or add new ones
        for cand in symbol:
            if cand.path in merged:
                existing = merged[cand.path]
                existing.symbols = list(dict.fromkeys(existing.symbols + cand.symbols))
                for r in cand.ranges:
                    existing.add_range(r[0], r[1])
                # Boost score: files found by both routes are more relevant
                existing.score = max(existing.score, cand.score) + 0.3
                existing.channel_scores["symbol"] = max(
                    existing.channel_scores.get("symbol", 0.0),
                    cand.channel_scores.get("symbol", 0.0),
                )
            else:
                # Symbol-only candidates: ensure a minimum score so they
                # can compete with vector candidates for budget slots.
                # Without this, files found late in the symbol substring
                # query (rank 40+) get negligible scores (~0.02) and are
                # always pushed out by vector results.
                if cand.score < 0.3:
                    cand.score = 0.3
                merged[cand.path] = cand
        # File-name bonus: files found by BOTH routes whose basename contains
        # a query identifier get a strong boost. These are the primary location
        # for the query concept (e.g. "session" in "session.py"). Only applied
        # to both-route files to avoid displacing vector results with
        # symbol-only matches that happen to have a matching basename.
        if query_identifiers:
            # Also try singular forms for file-name matching
            merge_idents = list(query_identifiers)
            for ident in query_identifiers:
                if len(ident) > 4 and ident.endswith("s"):
                    singular = ident[:-1]
                    if singular not in merge_idents:
                        merge_idents.append(singular)
            symbol_paths = {c.path for c in symbol}
            vector_paths = {c.path for c in vector}
            both_routes = symbol_paths & vector_paths
            for path in both_routes:
                candidate = merged[path]
                file_basename = path.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
                if any(ident.lower() in file_basename for ident in merge_idents):
                    candidate.score += 1.5
        # Sort by score descending, then by path for determinism
        result = sorted(merged.values(), key=lambda c: (-c.score, c.path))
        return result[:budget]

    async def _known_symbol_route(
        self, query: str, repository: Any | None, candidate_budget: int
    ) -> list[RetrievalCandidate]:
        """Known identifiers are a lexical lookup, never an embedding round trip.

        Two passes: exact name match first (highest confidence), then substring
        match for natural-language queries that mention concept words like
        "revision" or "freshness" that appear inside symbol names.
        """
        # Take up to 12 identifiers: first 8 with len>=4, extra 4 with len>=9.
        # The stricter filter for positions 9-12 avoids noise from common
        # programming terms (e.g. "backfill", "blocked") while still
        # capturing concept words like "middleware" that appear beyond
        # position 8 in longer queries.
        # Filter out very short or stopword-like identifiers that produce noise.
        _STOP = {
            "the", "and", "for", "with", "from", "that", "this", "what", "why",
            "how", "when", "where", "did", "does", "was", "were", "are", "not",
            "but", "all", "any", "can", "has", "have", "had", "will", "would",
            "could", "should", "into", "than", "then", "them", "they", "their",
            "there", "here", "which", "who", "whom", "its", "our", "your", "you",
            "she", "him", "her", "his", "get", "set", "put", "run", "use", "try",
            "new", "old", "one", "two", "three", "job", "fail", "deploy",
            # Project-specific: these match too many symbols via substring
            "brain", "project", "vector", "embedding", "revision", "comparison",
            "handle", "across", "explain",
            # Generic programming terms that match too many symbols
            "request", "user", "trace", "flows", "flow", "down",
        }
        # Filter stop words BEFORE limiting, so they don't consume slots.
        all_identifiers = list(dict.fromkeys(_IDENTIFIER_RE.findall(query)))
        raw_identifiers = [i for i in all_identifiers if i.lower() not in _STOP][:14]
        if not raw_identifiers:
            return []
        identifiers = []
        for idx, ident in enumerate(raw_identifiers):
            min_len = 4 if idx < 8 else 7
            if len(ident) >= min_len:
                identifiers.append(ident)
        if not identifiers:
            return []
        async with async_session_factory() as session:
            # Pass 1: exact name match
            stmt = (
                select(Symbol, File.path)
                .join(File, Symbol.file_id == File.id)
                .where(Symbol.name.in_(identifiers))
            )
            if repository is not None:
                stmt = stmt.where(File.repository_id == repository.id)
            rows = list((await session.execute(
                stmt.order_by(*symbol_relevance_order(identifiers)).limit(candidate_budget * 8)
            )).all())
            # Pass 2: substring match for concept words (only if pass 1 was sparse)
            if len(rows) < candidate_budget:
                # Also try singular forms (strip trailing 's') so that
                # "incidents" matches "IncidentState", "indices" matches
                # "IndexBuilder", etc.
                substr_idents = list(identifiers)
                for ident in identifiers:
                    if len(ident) > 4 and ident.endswith("s"):
                        singular = ident[:-1]
                        if singular not in substr_idents:
                            substr_idents.append(singular)
                substr_clauses = [Symbol.name.ilike(f"%{ident}%") for ident in substr_idents]
                if substr_clauses:
                    stmt2 = (
                        select(Symbol, File.path)
                        .join(File, Symbol.file_id == File.id)
                        .where(or_(*substr_clauses))
                        # Route paths (starting with /) and very short names
                        # produce too much noise in substring matching.
                        .where(~Symbol.name.like("/%"))
                    )
                    if repository is not None:
                        stmt2 = stmt2.where(File.repository_id == repository.id)
                    stmt2 = stmt2.order_by(*symbol_relevance_order(identifiers)).limit(candidate_budget * 10)
                    rows2 = list((await session.execute(stmt2)).all())
                    # Merge, avoiding duplicates; prefer symbols whose name
                    # matches multiple query terms (higher signal).
                    seen = {(s.name, p) for s, p in rows}
                    for row2 in rows2:
                        sym, path = row2
                        if (sym.name, path) not in seen:
                            rows.append(row2)
                            seen.add((sym.name, path))
        grouped: dict[str, RetrievalCandidate] = {}
        for rank, (symbol, path) in enumerate(rows, 1):
            candidate = grouped.setdefault(path, RetrievalCandidate(path=path))
            candidate.symbols.append(symbol.name)
            candidate.add_range(symbol.start_line, symbol.end_line)
            # Exact matches rank higher than substring matches
            is_exact = symbol.name in identifiers
            score = (1.0 / rank) * (1.5 if is_exact else 0.8)
            candidate.channel_scores["symbol"] = max(candidate.channel_scores.get("symbol", 0.0), score)
            candidate.score = max(candidate.score, score)
        # File-name bonus: files whose basename contains a query identifier
        # get a boost to help them make the top-N symbol candidates. Without
        # this, primary files (e.g. session.py) rank below files with many
        # substring matches (e.g. dashboard_auth.py with 7 matches) even
        # though they are the primary location for the query concept.
        # Also try singular forms (strip trailing 's') so "incidents" matches
        # "incidents.py" and "incident" in symbol names.
        file_name_idents = list(identifiers)
        for ident in identifiers:
            if len(ident) > 4 and ident.endswith("s"):
                singular = ident[:-1]
                if singular not in file_name_idents:
                    file_name_idents.append(singular)
        for path, candidate in grouped.items():
            parts = path.rsplit("/", 1)
            file_basename = parts[-1].rsplit(".", 1)[0].lower()
            parent_dir = parts[-2].lower() if len(parts) > 1 else ""
            if any(ident.lower() in file_basename or ident.lower() in parent_dir for ident in file_name_idents):
                candidate.score += 0.5
        return sorted(grouped.values(), key=lambda item: (-item.score, item.path))[:candidate_budget]

    @staticmethod
    def _normalize(raw: dict[str, Any], candidate_budget: int) -> list[RetrievalCandidate]:
        grouped: dict[str, RetrievalCandidate] = {}

        def candidate_for(path: str) -> RetrievalCandidate:
            return grouped.setdefault(path, RetrievalCandidate(path=path))

        for rank, item in enumerate(raw.get("files") or [], 1):
            if not isinstance(item, dict) or not item.get("path"):
                continue
            candidate = candidate_for(str(item["path"]))
            candidate.channel_scores["lexical"] = max(candidate.channel_scores.get("lexical", 0.0), 1.0 / rank)
            candidate.score += 1.0 / rank
        for rank, item in enumerate(raw.get("symbols") or [], 1):
            if not isinstance(item, dict) or not item.get("path"):
                continue
            candidate = candidate_for(str(item["path"]))
            if item.get("name"):
                candidate.symbols.append(str(item["name"]))
            candidate.add_range(item.get("start_line"), item.get("end_line"))
            candidate.channel_scores["symbol"] = max(candidate.channel_scores.get("symbol", 0.0), 1.0 / rank)
            candidate.score += 1.0 / rank
        for rank, item in enumerate(raw.get("chunks") or [], 1):
            if not isinstance(item, dict) or not item.get("file_path"):
                continue
            candidate = candidate_for(str(item["file_path"]))
            candidate.add_range(item.get("start_line"), item.get("end_line"))
            score = float(item.get("similarity") or 1.0 / rank)
            candidate.channel_scores["vector"] = max(candidate.channel_scores.get("vector", 0.0), score)
            candidate.score += score
        return sorted(grouped.values(), key=lambda item: (-item.score, item.path))[:candidate_budget]
