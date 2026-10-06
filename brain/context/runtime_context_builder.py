"""Ephemeral, bounded code slices for an active editing or answering task."""
from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path
from typing import Any

from sqlalchemy import select

from brain.config.settings import settings
from brain.context.budget import BudgetedPayloadBuilder, truncate_utf8
from brain.database.models import File, FileChunk, Symbol
from brain.database.session import async_session_factory
from brain.memory.relevance import select_relevant_normative_memory
from brain.retrieval.service import RetrievalResult, RetrievalService


class RuntimeContextBuilder:
    """Create an in-memory v2 context without writing a pack, file or DB row."""

    def __init__(self, retrieval: RetrievalService | None = None) -> None:
        self.retrieval = retrieval or RetrievalService()

    async def build(
        self,
        task_description: str,
        repo_path: Path | str,
        *,
        max_tokens: int = 3500,
        include_debug: bool = False,
    ) -> dict[str, Any]:
        from brain.context.context_cache import get_cached_context, put_cached_context

        repo_str = str(repo_path)
        cached = get_cached_context(repo_str, task_description)
        if cached is not None:
            res_cached = dict(cached)
            res_cached["_cache_hit"] = True
            return res_cached

        max_bytes = min(settings.AGENT_RUNTIME_CONTEXT_MAX_BYTES, max(1, max_tokens) * 4)
        result = await asyncio.wait_for(
            self.retrieval.retrieve(
                task_description,
                repo_path,
                intent="runtime_context",
                candidate_budget=12,
                deadline_s=settings.AGENT_CONTEXT_DEADLINE_S,
            ),
            timeout=settings.AGENT_CONTEXT_DEADLINE_S,
        )
        repo = self._repo_projection(result)
        freshness = repo["freshness"]
        metadata: dict[str, Any] = {"status": "ok", "repo": repo, "missing": list(result.degraded)}
        # A code-changing task cannot use an unprovably current index as edit
        # context. This is fail-closed, not a cosmetic warning.
        # BUT: kick off a background reindex so the next request is fresh.
        # The nightly auto-heal is too slow for active development (multiple
        # commits per day).
        if freshness != "current":
            metadata["status"] = "stale_blocked"
            metadata["missing"].append("current_source_required_for_code_change")
            # Fire-and-forget: enqueue reindex so next call succeeds.
            try:
                from brain.database.session import redis_client
                from brain.workers.queue import JobQueue
                queue = JobQueue(redis_client, prefix=settings.WORKER_REDIS_PREFIX)
                # Idempotency: one reindex per repo per hour max (enforced by
                # the queue's idempotency key TTL).
                await queue.enqueue(
                    "reindex",
                    {"repo_path": str(repo_path)},
                    idempotency_key=f"auto-reindex:{repo_path}",
                )
                metadata["missing"].append("auto_reindex_queued")
            except Exception:
                pass  # Best effort; stale_blocked is still honest.
            return BudgetedPayloadBuilder(max_bytes, metadata=metadata).build()

        # A current index with no retrieval evidence is not a usable context.
        # In particular, /ask must not silently turn an empty candidate list
        # into an LLM prompt that looks authoritative.
        if not result.candidates:
            metadata["status"] = "partial"
            metadata["missing"].append("no_retrieval_candidates")
            return BudgetedPayloadBuilder(max_bytes, metadata=metadata).build()

        slices = await self._load_slices(result, max_bytes)
        import logging as _lg
        _lg.getLogger(__name__).warning("SLICES_LOADED: count=%d max_bytes=%d", len(slices), max_bytes)
        if not slices:
            metadata["status"] = "partial"
            metadata["missing"].append("no_code_slices")
            return BudgetedPayloadBuilder(max_bytes, metadata=metadata).build()

        memory = await select_relevant_normative_memory(
            task_description,
            [candidate.path for candidate in result.candidates],
            repo.get("path"),
        )
        builder = BudgetedPayloadBuilder(max_bytes, metadata=metadata)
        # Include only top 6 candidates in context pack to save tokens.
        # All 12 candidates are still used for slice loading above.
        builder.add("candidates", [candidate.to_locator_dict() for candidate in result.candidates[:6]], priority=4)
        builder.add("slices", slices, priority=3)
        builder.add("memory", memory, priority=1)
        if include_debug:
            builder.add("debug", {"timings_ms": result.timings_ms, "degraded": result.degraded}, priority=-1)
        built = builder.build()
        if freshness == "current":
            put_cached_context(repo_str, task_description, built, source_revision=repo.get("source_revision"))
        return built

    @staticmethod
    def _repo_projection(result: RetrievalResult) -> dict[str, Any]:
        source = result.repository
        freshness = source.get("freshness") or {}
        return {
            "path": source.get("repository_path") or source.get("requested_path"),
            "indexed_revision": source.get("indexed_revision") or source.get("last_indexed_commit"),
            "source_revision": freshness.get("source_head_commit"),
            "freshness": freshness.get("status") or "unknown",
        }

    async def _load_slices(self, result: RetrievalResult, max_bytes: int) -> list[dict[str, Any]]:
        # Preserve candidate rank so the highest-scored file gets its slices
        # loaded first, not whatever happens to sort first alphabetically.
        ranked_paths = list(dict.fromkeys(candidate.path for candidate in result.candidates))
        repository_id = result.repository.get("repository_id")
        if not ranked_paths or not isinstance(repository_id, int):
            return []
        desired_ranges = {candidate.path: set(candidate.ranges) for candidate in result.candidates}
        # Keep original (matched) ranges separate for chunk priority sorting.
        # Matched ranges come from the symbol route (symbols that actually match
        # query identifiers). Enriched ranges (added later) include ALL symbols
        # in the file, which helps with coverage but is lower priority.
        original_ranges = {candidate.path: set(candidate.ranges) for candidate in result.candidates}
        # "Primary" files are those whose basename contains a query identifier
        # — e.g. "session" in "session.py" matches identifier "session". These
        # files are the primary location for the query concept and get a higher
        # slice limit to ensure full coverage of all evidence in the file.
        # Uses the same identifier extraction as the symbol route.
        _STOP_WORDS = {
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
        _ident_re = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b")
        query_identifiers = [
            w for w in _ident_re.findall(result.query)
            if len(w) >= 3 and w.lower() not in _STOP_WORDS
        ][:12]
        # Also try singular forms (strip trailing 's') so "incidents"
        # matches "incidents.py" and "indices" matches "index".
        primary_idents = list(query_identifiers)
        for ident in query_identifiers:
            if len(ident) > 4 and ident.endswith("s"):
                singular = ident[:-1]
                if singular not in primary_idents:
                    primary_idents.append(singular)
        primary_files: set[str] = set()
        strong_primary: set[str] = set()  # basename match — gets more slices
        # First pass: match basename only (high confidence — the file name
        # itself contains a query identifier, e.g. "session" in session.py).
        for candidate in result.candidates:
            basename = candidate.path.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
            if any(ident.lower() in basename for ident in primary_idents):
                primary_files.add(candidate.path)
                strong_primary.add(candidate.path)
        # Second pass: match parent directory (e.g. "llm" in path "brain/llm/
        # router.py" matches identifier "LLM"). This is needed for files like
        # router.py whose basename doesn't contain a query identifier but
        # whose package directory does.
        for candidate in result.candidates:
            if candidate.path in primary_files:
                continue
            parts = candidate.path.rsplit("/", 1)
            parent_dir = parts[-2].lower() if len(parts) > 1 else ""
            if any(ident.lower() in parent_dir for ident in primary_idents):
                primary_files.add(candidate.path)
        async with async_session_factory() as session:
            # Enrich ranges: for files found by symbol route, also include
            # ALL symbol ranges from that file. This ensures that evidence
            # like init_db (which doesn't match query identifiers but is a
            # key symbol in the file) gets its chunk loaded as overlapping.
            files_with_ranges = list(primary_files)
            if files_with_ranges:
                sym_rows = list(
                    (await session.execute(
                        select(Symbol.start_line, Symbol.end_line, File.path)
                        .join(File, Symbol.file_id == File.id)
                        .where(File.repository_id == repository_id, File.path.in_(files_with_ranges))
                    )).all()
                )
                for start, end, path in sym_rows:
                    if start is not None and end is not None:
                        desired_ranges[path].add((start, end))
            rows = list(
                (await session.execute(
                    select(FileChunk, File.path)
                    .join(File, FileChunk.file_id == File.id)
                    # Relative paths are only unique inside one repository.
                    # Never permit a code slice from another indexed project.
                    .where(File.repository_id == repository_id, File.path.in_(ranked_paths))
                    .order_by(File.path, FileChunk.start_line, FileChunk.chunk_index)
                )).all()
            )

        # Group chunks by file path, sorted within each file by 3-level priority:
        # 0 = overlaps original matched range (symbol route match, highest priority)
        # 1 = overlaps enriched range (all symbols in file, medium priority)
        # 2 = no overlap (lowest priority)
        # Within each priority, sort by line number.
        def _overlap_level(chunk, orig_ranges, all_ranges):
            if chunk.start_line is None or chunk.end_line is None:
                return 2
            if orig_ranges and any(chunk.start_line <= end and start <= chunk.end_line for start, end in orig_ranges):
                return 0
            if all_ranges and any(chunk.start_line <= end and start <= chunk.end_line for start, end in all_ranges):
                return 1
            return 2

        chunks_by_path: dict[str, list[tuple[Any, int]]] = {}
        for chunk, path in rows:
            orig = original_ranges.get(path, set())
            all_r = desired_ranges.get(path, set())
            chunks_by_path.setdefault(path, []).append((chunk, _overlap_level(chunk, orig, all_r)))

        for path in chunks_by_path:
            chunks_by_path[path].sort(key=lambda x: (x[1], x[0].start_line or 0, x[0].chunk_index or 0))

        # Hybrid allocation: first pass gives 1 slice per file (round-robin
        # by rank) so every candidate gets coverage. Second pass fills
        # remaining budget rank-ordered, with a higher per-file limit for
        # files that have symbol ranges (these are "primary" files for the
        # query concept and need more coverage to capture all evidence).
        slices: list[dict[str, Any]] = []
        seen_hashes: set[str] = set()
        slices_per_file: dict[str, int] = {}
        max_total_slices = 22

        ordered_paths = [p for p in ranked_paths if p in chunks_by_path]
        next_idx: dict[str, int] = {p: 0 for p in ordered_paths}

        def _try_take(path: str) -> bool:
            """Try to take the next usable chunk for path. Returns True if taken."""
            chunk_list = chunks_by_path[path]
            while next_idx[path] < len(chunk_list):
                chunk, overlaps = chunk_list[next_idx[path]]
                next_idx[path] += 1
                content_text = chunk.content or ""
                content_stripped = content_text.strip()
                if not content_stripped or len(content_stripped) < 3:
                    continue
                content_hash = hashlib.sha256(content_stripped.encode("utf-8")).hexdigest()
                if content_hash in seen_hashes:
                    continue
                seen_hashes.add(content_hash)
                slices_per_file[path] = slices_per_file.get(path, 0) + 1
                slices.append({
                    "path": path,
                    "range": [chunk.start_line, chunk.end_line],
                    "content": truncate_utf8(chunk.content, min(2_200, max_bytes // 4)),
                })
                return True
            return False

        # Pass 1: 1 slice per file (round-robin by rank) so every candidate
        # gets at least some coverage, even if primary files later take
        # most of the budget.
        primary_ordered = [p for p in ordered_paths if p in primary_files]
        non_primary_ordered = [p for p in ordered_paths if p not in primary_files]
        for path in ordered_paths:
            if len(slices) >= max_total_slices:
                break
            _try_take(path)

        # Pass 1b: give each primary file a 2nd slice so that evidence
        # in the 2nd-priority chunk (e.g. DEFAULT_LLM_PROVIDER in router.py
        # chunk 3) is loaded even if pass 2 budget is consumed by higher-
        # ranked primary files.
        for path in primary_ordered:
            if len(slices) >= max_total_slices:
                break
            _try_take(path)

        # Pass 2: fill primary files first (rank-ordered), then non-primary
        # files. Strong primary files (basename match) get up to 8 slices
        # — they are the most likely location for key evidence. Weak primary
        # files (parent dir match only) get up to 6 slices. Non-primary
        # files get up to 3 slices.
        for path in primary_ordered:
            if len(slices) >= max_total_slices:
                break
            file_limit = 8 if path in strong_primary else 6
            while slices_per_file.get(path, 0) < file_limit and len(slices) < max_total_slices:
                if not _try_take(path):
                    break
        for path in non_primary_ordered:
            if len(slices) >= max_total_slices:
                break
            while slices_per_file.get(path, 0) < 3 and len(slices) < max_total_slices:
                if not _try_take(path):
                    break
        return slices
