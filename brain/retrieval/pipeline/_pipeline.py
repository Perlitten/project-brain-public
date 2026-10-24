"""The hybrid retrieval pipeline orchestrator."""
from __future__ import annotations

import asyncio
import copy
import time
from typing import Dict, List, Optional
from loguru import logger
from sqlalchemy import case, or_, select
from brain.config.settings import settings
from brain.context.context_pack_builder import StableMemoryCache
from brain.database.models import File, Repository, Symbol
from brain.database.session import async_session_factory
from brain.graph.graph_client import GraphClient
from brain.retrieval.fusion import fuse_candidates, prepare_channels, rank_channel_paths
from brain.retrieval.graph_extractors import retrieval_tab_core_boost
from brain.late_interaction.application import apply_late_interaction, late_interaction_requested
from brain.retrieval.reranker import RerankResult, rerank_top_k
from brain.retrieval.types import (ChannelCandidate, QueryRoute, RecallStageResult, RetrievalPipelineResult, RetrievalTiming, SurfacePool)
from brain.search.code_search import vector_search_cards, vector_search_chunks
from brain.search.filters import (HISTORICAL_SUMMARY_PREFIX, expand_protected_pairs, is_extension_noise_path, is_extension_tab_path, is_root_config_path, is_test_path, knowledge_authority_weight, paired_test_path, should_exclude_from_retrieval, query_requests_historical_context)
from brain.search.path_hints import derive_path_hints, expand_keywords, path_hint_bonus
from brain.search.lexical_ranking import probe_paths_for_keywords
from brain.search.surfaces import IMPORT_TARGET_KIND, V6_SYMBOL_KINDS, classify_surface
from brain.search.task_intent import audit_test_boost_paths, derive_task_intent
from ._channels import (_lexical_file_candidate, _path_keyword_hits, _should_abstain, _synth_pool_candidate, classify_query_route, select_surface_balanced_pool)
from ._fusion import _fusion_policy
from ._late import (_apply_late_interaction_scores, _build_rerank_pool, _rerank_late_counterfactual, _resolve_late_interaction_candidates)
from ._select import _pin_qualified_path_hints, _select_final_paths
from ._terms import _content_bm25_channel, extract_code_query_terms


class HybridRetrievalPipeline:
    """Orchestrates multi-channel candidate generation, fusion, and reranking."""

    def __init__(self, cache: Optional[StableMemoryCache] = None):
        self.cache = cache or StableMemoryCache()

    async def run(
        self,
        task_description: str,
        task_type: str,
        keywords: List[str],
        repository_id: Optional[int],
        repository_name: str,
        file_limit: int = 10,
        retrieval_mode: str = "fast",
        late_interaction_client=None,
    ) -> RetrievalPipelineResult:
        if retrieval_mode not in {"fast", "deep"}:
            raise ValueError("retrieval_mode must be 'fast' or 'deep'")
        deep_mode = retrieval_mode == "deep"
        timing = RetrievalTiming()
        t0 = time.perf_counter()
        route = classify_query_route(task_type, keywords, task_description)
        intent = derive_task_intent(task_description, task_type=task_type)
        keywords_lower = expand_keywords(keywords, task_description)
        v2_enabled = bool(settings.RETRIEVAL_V2_ENABLED)
        # v2 (d): mine issue text for code-shaped terms (paths, stack frames,
        # identifiers, error names) and feed the lexical/symbol/hints channels.
        if v2_enabled and settings.RETRIEVAL_V2_ID_QUERY:
            for term in extract_code_query_terms(task_description):
                tl = term.lower()
                if tl not in keywords_lower:
                    keywords_lower.append(tl)
        # v6 (P0): surface-aware recall + top-100 pool. Gated to deep budgets so
        # small-budget packs keep the frozen v5 path byte-for-byte.
        v6_enabled = bool(settings.RETRIEVAL_V6_ENABLED) and file_limit >= 10
        expected_surface = intent.expected_surface if v6_enabled else ""
        cards_enabled = v6_enabled and bool(settings.RETRIEVAL_V6_FILE_CARDS_ENABLED)
        card_recall = cards_enabled and bool(settings.RETRIEVAL_V6_CARD_RECALL)
        card_rerank = cards_enabled and bool(settings.RETRIEVAL_V6_CARD_RERANK)
        timing.query_parsing_ms = (time.perf_counter() - t0) * 1000

        candidates_by_channel: Dict[str, List[ChannelCandidate]] = {
            "lexical": [],
            "symbol": [],
            "hints": [],
            "vector": [],
            "graph": [],
            "memory": [],
            "card": [],
        }

        (lexical_files, lexical_symbols, symbol_paths, hint_files) = await self._stage_lexical_symbols(
            keywords_lower=keywords_lower, repository_id=repository_id, task_description=task_description, v6_enabled=v6_enabled, candidates_by_channel=candidates_by_channel, timing=timing)
        (vector_result, chunk_files) = await self._stage_vector(
            task_description=task_description, deep_mode=deep_mode, v6_enabled=v6_enabled, repository_id=repository_id, v2_enabled=v2_enabled, candidates_by_channel=candidates_by_channel, timing=timing)
        (card_by_path) = await self._stage_cards(
            task_description=task_description, repository_id=repository_id, card_recall=card_recall, card_rerank=card_rerank, candidates_by_channel=candidates_by_channel, timing=timing)
        (graph_counts) = await self._stage_graph(
            repository_id=repository_id, route=route, keywords_lower=keywords_lower, candidates_by_channel=candidates_by_channel, timing=timing)
        await self._stage_memory(
            repository_id=repository_id, keywords_lower=keywords_lower, candidates_by_channel=candidates_by_channel, timing=timing)
        await self._stage_bm25_fallback(
            v2_enabled=v2_enabled, repository_id=repository_id, task_description=task_description, candidates_by_channel=candidates_by_channel)
        (prepared_fusion, fused_ranking, channel_paths, path_to_file) = await self._stage_fusion(
            candidates_by_channel=candidates_by_channel, route=route, card_recall=card_recall, lexical_files=lexical_files, hint_files=hint_files, chunk_files=chunk_files, card_by_path=card_by_path, repository_id=repository_id, v2_enabled=v2_enabled, v6_enabled=v6_enabled, expected_surface=expected_surface, task_type=task_type, task_description=task_description, keywords_lower=keywords_lower, timing=timing)
        (reranked, probe_list, hint_tokens, v2_rerank_off) = await self._stage_rerank_boosts(
            fused_ranking=fused_ranking, keywords_lower=keywords_lower, task_description=task_description, intent=intent, candidates_by_channel=candidates_by_channel, graph_counts=graph_counts, route=route, path_to_file=path_to_file, task_type=task_type, v2_enabled=v2_enabled, timing=timing)
        (recall, pool_candidates) = await self._stage_recall_pool(
            v6_enabled=v6_enabled, reranked=reranked, expected_surface=expected_surface, file_limit=file_limit, repository_id=repository_id, path_to_file=path_to_file, intent=intent, task_type=task_type, timing=timing)
        (precision_k, commit_hash, v5_result, baseline_v5_top, available, vector_by_path, lexical_by_path, symbol_by_path, graph_by_path) = await self._stage_v5_rerank(
            candidates_by_channel=candidates_by_channel, pool_candidates=pool_candidates, path_to_file=path_to_file, card_by_path=card_by_path, card_rerank=card_rerank, file_limit=file_limit, repository_id=repository_id, v2_rerank_off=v2_rerank_off, reranked=reranked, task_description=task_description, task_type=task_type, repository_name=repository_name, v6_enabled=v6_enabled, expected_surface=expected_surface, timing=timing)
        (pinned, baseline_selected) = await self._stage_baseline_selection(
            probe_list=probe_list, v2_rerank_off=v2_rerank_off, repository_id=repository_id, path_to_file=path_to_file, hint_tokens=hint_tokens, file_limit=file_limit, candidates_by_channel=candidates_by_channel, reranked=reranked, baseline_v5_top=baseline_v5_top, precision_k=precision_k, keywords_lower=keywords_lower, task_type=task_type, task_description=task_description, timing=timing)
        (selected, v5_top, v5_debug_result, reranked, late_debug) = await self._stage_late_interaction(
            repository_id=repository_id, deep_mode=deep_mode, task_description=task_description, pool_candidates=pool_candidates, vector_result=vector_result, reranked=reranked, path_to_file=path_to_file, vector_by_path=vector_by_path, lexical_by_path=lexical_by_path, symbol_by_path=symbol_by_path, graph_by_path=graph_by_path, card_by_path=card_by_path, card_rerank=card_rerank, available=available, precision_k=precision_k, expected_surface=expected_surface, v6_enabled=v6_enabled, commit_hash=commit_hash, task_type=task_type, repository_name=repository_name, file_limit=file_limit, candidates_by_channel=candidates_by_channel, keywords_lower=keywords_lower, pinned=pinned, baseline_selected=baseline_selected, baseline_v5_top=baseline_v5_top, v5_result=v5_result, late_interaction_client=late_interaction_client, timing=timing)

        abstained = _should_abstain(
            selected=selected,
            reranked=reranked,
            candidates_by_channel=candidates_by_channel,
            task_type=task_type,
            keywords=keywords_lower,
        )
        if abstained:
            selected = []

        seen_selected = set(selected)
        fallback_reason = "hybrid_rrf_v5_rerank"
        for cand in reranked:
            cand.included = cand.item_id in seen_selected
            if not cand.included:
                cand.exclusion_reason = "budget_cap"

        return RetrievalPipelineResult(
            route=route,
            vector_status=vector_result.status,
            candidates_by_channel=candidates_by_channel,
            fused_ranking=fused_ranking,
            reranked=reranked,
            selected_paths=selected,
            v5_top_paths=v5_top,
            pool_paths=recall.pool_paths,
            recall_debug=recall.to_debug(),
            v5_rerank_debug={
                "cache_hit": v5_debug_result.cache_hit,
                "cache_key": v5_debug_result.cache_key,
                "used_llm": v5_debug_result.used_llm,
                "fallback": v5_debug_result.fallback,
                "fallback_reason": v5_debug_result.fallback_reason,
                "stage": v5_debug_result.stage,
                "reranker_version": v5_debug_result.reranker_version,
                "abstained": abstained,
                "latency_ms": round(v5_debug_result.latency_ms, 2),
                "estimated_cost_usd": v5_debug_result.estimated_cost_usd,
                "position_changes": v5_debug_result.position_changes,
                "excluded_relevant": v5_debug_result.excluded_relevant,
                "explanations": {c.path: c.explanation for c in v5_debug_result.candidates},
                "confidence": {c.path: c.confidence for c in v5_debug_result.candidates},
            },
            timing=timing,
            debug={
                "route": route.value,
                "retrieval_mode": retrieval_mode,
                "vector_message": vector_result.message,
                "channel_counts": {k: len(v) for k, v in candidates_by_channel.items()},
                "selection_mode": fallback_reason,
                "best_channel": None,
                "channel_path_counts": {k: len(v) for k, v in channel_paths.items()},
                "v6_enabled": v6_enabled,
                "expected_surface": expected_surface,
                "recall": recall.to_debug(),
                "cards": {
                    "enabled": cards_enabled,
                    "card_recall": card_recall,
                    "card_rerank": card_rerank,
                    "card_matches": len(card_by_path),
                    "card_scores": {
                        p: round(s, 4) for p, s in sorted(card_by_path.items(), key=lambda x: x[1], reverse=True)[:15]
                    },
                },
                "late_interaction": late_debug,
            },
        )


    async def _stage_lexical_symbols(self, *, keywords_lower, repository_id, task_description, v6_enabled, candidates_by_channel, timing):
        # Lexical + symbol
        t_lex = time.perf_counter()
        lexical_files: List[File] = []
        lexical_symbols: List[Symbol] = []
        symbol_paths: Dict[int, str] = {}
        hint_files: List[File] = []
        async with async_session_factory() as session:
            if keywords_lower:
                file_clauses = [File.path.ilike(f"%{kw}%") for kw in keywords_lower] + [
                    File.summary.ilike(f"%{kw}%") for kw in keywords_lower
                ]
                historical_order = case(
                    (File.summary.startswith(HISTORICAL_SUMMARY_PREFIX), 1),
                    else_=0,
                )
                if query_requests_historical_context(task_description):
                    historical_order = case(
                        (File.summary.startswith(HISTORICAL_SUMMARY_PREFIX), 0),
                        else_=1,
                    )
                stmt = (
                    select(File)
                    .where(or_(*file_clauses))
                    .order_by(historical_order, File.path)
                    .limit(160 if v6_enabled else 80)
                )
                if repository_id is not None:
                    stmt = stmt.where(File.repository_id == repository_id)
                lexical_files = [
                    f
                    for f in (await session.execute(stmt)).scalars().all()
                    if not should_exclude_from_retrieval(f.path)
                ]
                lexical_files.sort(
                    key=lambda f: (
                        knowledge_authority_weight(f.summary, task_description),
                        _path_keyword_hits(f.path, keywords_lower, task_description),
                    ),
                    reverse=True,
                )

                sym_clauses = [Symbol.name.ilike(f"%{kw}%") for kw in keywords_lower]
                # v6 P1: structural symbol kinds (routes/CLI/entrypoints) join the
                # lexical channel only when v6 is on; import_target is never lexical.
                # When off, this keeps the channel byte-identical to frozen v5.
                excluded_kinds = {IMPORT_TARGET_KIND}
                if not v6_enabled:
                    excluded_kinds |= set(V6_SYMBOL_KINDS)
                stmt_s = (
                    select(Symbol, File.path)
                    .join(File)
                    .where(or_(*sym_clauses))
                    .where(or_(Symbol.kind.is_(None), Symbol.kind.notin_(excluded_kinds)))
                    .limit(50 if v6_enabled else 30)
                )
                if repository_id is not None:
                    stmt_s = stmt_s.where(File.repository_id == repository_id)
                for sym, file_path in (await session.execute(stmt_s)).all():
                    if should_exclude_from_retrieval(file_path):
                        continue
                    lexical_symbols.append(sym)
                    symbol_paths[sym.id] = file_path

                for hint in derive_path_hints(task_description):
                    stmt_h = select(File).where(File.path.ilike(f"%{hint}%")).limit(10)
                    if repository_id is not None:
                        stmt_h = stmt_h.where(File.repository_id == repository_id)
                    hint_files.extend(
                        f
                        for f in (await session.execute(stmt_h)).scalars().all()
                        if not should_exclude_from_retrieval(f.path)
                    )

                probe_paths = probe_paths_for_keywords(keywords_lower, task_description)
                if probe_paths:
                    stmt_p = select(File).where(File.path.in_(probe_paths))
                    if repository_id is not None:
                        stmt_p = stmt_p.where(File.repository_id == repository_id)
                    for f in (await session.execute(stmt_p)).scalars().all():
                        if not should_exclude_from_retrieval(f.path):
                            hint_files.insert(0, f)
        timing.lexical_ms = (time.perf_counter() - t_lex) * 1000
        timing.symbol_ms = timing.lexical_ms * 0.3

        for rank, f in enumerate(lexical_files, 1):
            candidates_by_channel["lexical"].append(_lexical_file_candidate(f, rank))
        for rank, sym in enumerate(lexical_symbols, 1):
            sym_file_path = symbol_paths.get(sym.id)
            if not sym_file_path:
                continue
            candidates_by_channel["symbol"].append(
                ChannelCandidate(
                    "symbol",
                    sym_file_path,
                    1.0 / rank,
                    rank=rank,
                    metadata={"symbol": sym.name, "kind": sym.kind},
                )
            )
        for rank, f in enumerate(hint_files, 1):
            candidates_by_channel["hints"].append(ChannelCandidate("hints", f.path, 2.0 / rank, rank=rank))
        return lexical_files, lexical_symbols, symbol_paths, hint_files
    async def _stage_vector(self, *, task_description, deep_mode, v6_enabled, repository_id, v2_enabled, candidates_by_channel, timing):
        # Vector
        t_vec = time.perf_counter()
        vector_limit = (
            settings.LATE_INTERACTION_DEEP_VECTOR_LIMIT
            if deep_mode
            else (80 if v6_enabled else 50)
        )
        vector_result = await vector_search_chunks(
            task_description,
            top_k=vector_limit,
            repository_id=repository_id,
        )
        timing.vector_ms = (time.perf_counter() - t_vec) * 1000
        chunk_files: Dict[int, File] = {}
        if vector_result.matches:
            file_ids = {f.id for _, _, f in vector_result.matches if f}
            if file_ids:
                async with async_session_factory() as session:
                    for f in (await session.execute(select(File).where(File.id.in_(list(file_ids))))).scalars():
                        chunk_files[f.id] = f
        seen_vec_sims: Dict[str, List[float]] = {}
        for rank, (sim, _chunk, file_obj) in enumerate(vector_result.matches, 1):
            if not file_obj:
                continue
            seen_vec_sims.setdefault(file_obj.path, []).append(sim)
        # v2 (e): chunk->file aggregation is a tunable ("max" | "sum" | "topk");
        # v5 keeps hardcoded max.
        vec_agg = settings.RETRIEVAL_V2_VEC_AGG if v2_enabled else "max"
        seen_vec_paths: Dict[str, float] = {}
        for path, sims in seen_vec_sims.items():
            if vec_agg == "sum":
                seen_vec_paths[path] = sum(sims)
            elif vec_agg == "topk":
                top = sorted(sims, reverse=True)[:3]
                seen_vec_paths[path] = sum(top) / len(top)
            else:
                # Legacy aggregation starts at zero, so negative-only matches
                # must keep that floor when the experiment is disabled.
                seen_vec_paths[path] = max(sims) if v2_enabled else max(0.0, max(sims))
        for rank, (path, sim) in enumerate(sorted(seen_vec_paths.items(), key=lambda x: x[1], reverse=True), 1):
            candidates_by_channel["vector"].append(ChannelCandidate("vector", path, sim, rank=rank))
        return vector_result, chunk_files
    async def _stage_cards(self, *, task_description, repository_id, card_recall, card_rerank, candidates_by_channel, timing):
        # Card vector (v6 P3) — file-role signal channel; gated, never card-first.
        card_by_path: Dict[str, float] = {}
        if card_recall or card_rerank:
            t_card = time.perf_counter()
            card_result = await vector_search_cards(task_description, top_k=80, repository_id=repository_id)
            for path, sim in [(p, s) for s, p in card_result.matches]:
                if sim > card_by_path.get(path, 0.0):
                    card_by_path[path] = sim
            if card_recall:
                for rank, (path, sim) in enumerate(sorted(card_by_path.items(), key=lambda x: x[1], reverse=True), 1):
                    candidates_by_channel["card"].append(ChannelCandidate("card", path, sim, rank=rank))
            timing.graph_expansion_ms += (time.perf_counter() - t_card) * 1000
        return card_by_path
    async def _stage_graph(self, *, repository_id, route, keywords_lower, candidates_by_channel, timing):
        # Graph (skip for routes/channels where it rarely helps latency or precision)
        t_graph = time.perf_counter()
        graph_counts: Dict[str, int] = {}
        if repository_id is not None and route in {QueryRoute.DEPENDENCY, QueryRoute.CROSS_SURFACE} and keywords_lower:
            graph_client = GraphClient(repository_id=repository_id)

            async def _graph_channel() -> None:
                for kw in keywords_lower[:3]:
                    try:
                        cypher = (
                            "MATCH (repo:Repository) "
                            "WHERE repo.repository_id = $repository_id "
                            "MATCH (repo)-[:CONTAINS*0..6]->(n) "
                            "WHERE n.repository_id = $repository_id "
                            "AND (toLower(n.name) CONTAINS toLower($kw) "
                            "OR (n.path IS NOT NULL AND toLower(n.path) CONTAINS toLower($kw))) "
                            "MATCH (n)-[r*1..2]-(m) "
                            "WHERE m.repository_id = $repository_id "
                            "RETURN m"
                        )
                        async with graph_client.driver.session() as neo_session:
                            result = await neo_session.run(
                                cypher,
                                kw=kw,
                                repository_id=repository_id,
                            )
                            async for record in result:
                                node = record["m"]
                                if "File" in list(node.labels):
                                    path = node.get("path") or node.get("name")
                                    if path and not should_exclude_from_retrieval(path):
                                        graph_counts[path] = graph_counts.get(path, 0) + 1
                    except Exception as exc:
                        logger.warning(f"Graph channel failed for '{kw}': {exc}")

            try:
                await asyncio.wait_for(_graph_channel(), timeout=settings.RETRIEVAL_TIMEOUT_NEO4J_S)
            except asyncio.TimeoutError:
                logger.warning(f"Graph channel timed out after {settings.RETRIEVAL_TIMEOUT_NEO4J_S}s")
        timing.neo4j_ms = (time.perf_counter() - t_graph) * 1000
        for rank, (path, count) in enumerate(sorted(graph_counts.items(), key=lambda x: x[1], reverse=True), 1):
            candidates_by_channel["graph"].append(ChannelCandidate("graph", path, float(count), rank=rank))
        return graph_counts
    async def _stage_memory(self, *, repository_id, keywords_lower, candidates_by_channel, timing):
        # Memory / cache
        t_mem = time.perf_counter()
        await self.cache.initialize(repository_id=repository_id)
        if self.cache.initialized:
            for kw in keywords_lower:
                for f in self.cache.files:
                    if kw in (f.path.lower() or "") and not should_exclude_from_retrieval(f.path):
                        candidates_by_channel["memory"].append(
                            ChannelCandidate("memory", f.path, 0.8, metadata={"source": "cache"})
                        )
        timing.memory_ms = (time.perf_counter() - t_mem) * 1000
    async def _stage_bm25_fallback(self, *, v2_enabled, repository_id, task_description, candidates_by_channel):
        # v2 (c): when every lexical-side channel came back empty (anonymous
        # corpora where path/symbol/hint matching cannot fire), fall back to a
        # content-BM25 channel so fusion still mixes a lexical signal with the
        # vector channel instead of diluting dense results.
        if v2_enabled and settings.RETRIEVAL_V2_BM25_FALLBACK:
            candidates_by_channel.setdefault("bm25", [])
            lexical_side_empty = not any(
                candidates_by_channel.get(ch)
                for ch in ("lexical", "symbol", "hints", "graph", "memory")
            )
            if lexical_side_empty:
                candidates_by_channel["bm25"] = await _content_bm25_channel(
                    repository_id, task_description
                )
    async def _stage_fusion(self, *, candidates_by_channel, route, card_recall, lexical_files, hint_files, chunk_files, card_by_path, repository_id, v2_enabled, v6_enabled, expected_surface, task_type, task_description, keywords_lower, timing):
        # Normalize per channel
        t_fusion = time.perf_counter()
        prepared_fusion = prepare_channels(candidates_by_channel, route, card_recall=card_recall)
        file_ranks = prepared_fusion.file_ranks

        path_to_file: Dict[str, File] = {f.path: f for f in lexical_files + hint_files}
        for f in chunk_files.values():
            path_to_file.setdefault(f.path, f)
        if card_by_path:
            missing_card = [p for p in card_by_path if p not in path_to_file]
            if missing_card:
                async with async_session_factory() as session:
                    stmt_c = select(File).where(File.path.in_(missing_card))
                    if repository_id is not None:
                        stmt_c = stmt_c.where(File.repository_id == repository_id)
                    for f in (await session.execute(stmt_c)).scalars().all():
                        path_to_file.setdefault(f.path, f)
        missing_ranked = [path for path in file_ranks if path not in path_to_file]
        if missing_ranked:
            async with async_session_factory() as session:
                stmt_ranked = select(File).where(File.path.in_(missing_ranked))
                if repository_id is not None:
                    stmt_ranked = stmt_ranked.where(File.repository_id == repository_id)
                for file_record in (await session.execute(stmt_ranked)).scalars().all():
                    path_to_file.setdefault(file_record.path, file_record)

        # v2 (a): dense-primary fusion — pure channel combination; the v5
        # post-RRF heuristics (contextual weight, content match, path-keyword
        # bonus, surface weight) are skipped so channels only boost what fired.
        fused_ranking = fuse_candidates(
            prepared_fusion, candidates_by_channel, path_to_file,
            policy=_fusion_policy(route, v2_enabled=v2_enabled, v6_enabled=v6_enabled,
                                  expected_surface=expected_surface),
            task_type=task_type, task_description=task_description, keywords_lower=keywords_lower,
        )
        timing.fusion_ms = (time.perf_counter() - t_fusion) * 1000

        channel_paths = rank_channel_paths(prepared_fusion, candidates_by_channel)
        return prepared_fusion, fused_ranking, channel_paths, path_to_file
    async def _stage_rerank_boosts(self, *, fused_ranking, keywords_lower, task_description, intent, candidates_by_channel, graph_counts, route, path_to_file, task_type, v2_enabled, timing):
        # Rerank: lexical + hint boosts for precision at top-k
        t_rerank = time.perf_counter()
        probe_list = probe_paths_for_keywords(keywords_lower, task_description)
        probe_list.extend(audit_test_boost_paths(task_description))
        if intent.wants_root_config:
            probe_list.append("pyproject.toml")
        probe_list = list(dict.fromkeys(probe_list))
        vector_top = {c.item_id: c.raw_score for c in candidates_by_channel["vector"][:5]}
        lexical_top = {c.item_id for c in candidates_by_channel["lexical"][:8]}
        symbol_top = {c.item_id for c in candidates_by_channel["symbol"][:6]}
        hint_tokens = set(derive_path_hints(task_description))
        pinned_set_preview = set(probe_list)
        reranked: List[ChannelCandidate] = []
        # v2 (b): the heuristic bonus stage is gated — when off, fusion order is
        # the ranking and selection falls back to fused top-k below.
        v2_rerank_off = v2_enabled and not settings.RETRIEVAL_V2_RERANK_ENABLED
        if v2_rerank_off:
            for cand in fused_ranking:
                cand.reranker_score = cand.normalized_score
            reranked = list(fused_ranking)
            timing.rerank_ms = (time.perf_counter() - t_rerank) * 1000
        else:
            for cand in fused_ranking:
                bonus = 0.0
                norm_path = cand.item_id.replace("\\", "/").lower()
                lex_score = _path_keyword_hits(cand.item_id, keywords_lower, task_description)
                bonus += min(lex_score * 0.05, 0.45)
                if cand.item_id in pinned_set_preview:
                    bonus += 0.55
                if is_root_config_path(cand.item_id) and intent.wants_root_config:
                    bonus += 0.42
                bonus += path_hint_bonus(cand.item_id, hint_tokens)
                if task_type == "bugfix" and "/test_" in norm_path:
                    bonus += 0.28
                if norm_path.endswith("test_audit_fixes.py") and any(
                    t in task_description.lower() for t in ("pillz", "audit", "recommendation")
                ):
                    bonus += 0.35
                if norm_path.endswith("test_e2e_stability.py") and "wire" in task_description.lower():
                    bonus += 0.32
                if cand.item_id in lexical_top:
                    bonus += 0.14
                if cand.item_id in symbol_top:
                    bonus += 0.22
                if cand.item_id in vector_top and route == QueryRoute.CONCEPTUAL:
                    bonus += vector_top[cand.item_id] * 0.15
                elif cand.item_id in vector_top and cand.item_id not in lexical_top and cand.item_id not in symbol_top:
                    if vector_top[cand.item_id] < 0.22:
                        bonus -= 0.18
                if cand.item_id in graph_counts and route == QueryRoute.DEPENDENCY:
                    bonus += 0.12
                if is_extension_tab_path(cand.item_id) and intent.wants_server_router:
                    if "battletab.tsx" not in norm_path:
                        bonus -= 0.25
                bonus += retrieval_tab_core_boost(cand.item_id, [c.item_id for c in fused_ranking[:15]], task_description)
                if is_extension_noise_path(cand.item_id) and not intent.wants_extension:
                    bonus -= 0.4
                if norm_path.startswith("scripts/") and not intent.wants_scripts:
                    if "backfill_market" in norm_path and intent.is_deletion:
                        bonus += 0.3
                    elif norm_path.endswith(".md") or "selfplay_harness" in norm_path:
                        bonus -= 0.3
                if norm_path.startswith("src/server/routers/") and intent.wants_server_router:
                    bonus += 0.2
                file_rec = path_to_file.get(cand.item_id)
                authority_weight = knowledge_authority_weight(
                    file_rec.summary if file_rec else None,
                    task_description,
                )
                cand.reranker_score = (cand.normalized_score + bonus) * authority_weight
                reranked.append(cand)
            reranked.sort(key=lambda c: c.reranker_score, reverse=True)
            timing.rerank_ms = (time.perf_counter() - t_rerank) * 1000
        return reranked, probe_list, hint_tokens, v2_rerank_off
    async def _stage_recall_pool(self, *, v6_enabled, reranked, expected_surface, file_limit, repository_id, path_to_file, intent, task_type, timing):
        # Recall stage (v6) → deterministic rerank → precision top-10.
        # v5 path: simple top-30 fused slice. v6 path: surface-balanced top-100
        # pool with colocated-test / docs-config co-rank injection.
        t_recall = time.perf_counter()
        recall = RecallStageResult(expected_surface=expected_surface)
        if v6_enabled:
            pool_limit = settings.RETRIEVAL_POOL_LIMIT
            min_quota = settings.RETRIEVAL_SURFACE_MIN_QUOTA
            pool_candidates, surface_pools = select_surface_balanced_pool(
                reranked, pool_limit, expected_surface, min_quota
            )
            pool_seen = {c.item_id for c in pool_candidates}

            inject_targets: List[str] = []
            top30_paths = [c.item_id for c in reranked[:30]]
            for cand in reranked[:30]:
                surf = classify_surface(cand.item_id)
                if surf in {"engine", "server", "database", "scripts"} and not is_test_path(cand.item_id):
                    test_path = paired_test_path(cand.item_id)
                    if test_path and test_path not in pool_seen:
                        inject_targets.append(test_path)
            if intent.task_type in {"database", "cross_surface"} or expected_surface in {"docs", "config"}:
                for seed in ("README.md", "pyproject.toml", "docker-compose.yml", "src/db/models.py"):
                    if seed not in pool_seen:
                        inject_targets.append(seed)
            # v6 P1: import-resolved co-rank — pull the source files that top-30 test/
            # script/src files import (TEST->SOURCE, SCRIPT->DOMAIN via import_target symbols).
            if top30_paths:
                async with async_session_factory() as session:
                    stmt_imp = (
                        select(Symbol.name)
                        .join(File)
                        .where(File.path.in_(top30_paths), Symbol.kind == IMPORT_TARGET_KIND)
                    )
                    if repository_id is not None:
                        stmt_imp = stmt_imp.where(File.repository_id == repository_id)
                    for (target,) in (await session.execute(stmt_imp)).all():
                        if target in pool_seen:
                            continue
                        if classify_surface(target) in {"engine", "server", "database", "scripts"}:
                            inject_targets.append(target)
            inject_targets = list(dict.fromkeys(inject_targets))[:40]

            floor = (pool_candidates[-1].reranker_score * 0.5) if pool_candidates else 0.0
            injected_paths: List[str] = []
            if inject_targets:
                async with async_session_factory() as session:
                    stmt_inj = select(File).where(File.path.in_(inject_targets))
                    if repository_id is not None:
                        stmt_inj = stmt_inj.where(File.repository_id == repository_id)
                    for f in (await session.execute(stmt_inj)).scalars().all():
                        if should_exclude_from_retrieval(f.path) or f.path in pool_seen:
                            continue
                        path_to_file.setdefault(f.path, f)
                        pool_candidates.append(_synth_pool_candidate(f.path, floor, "co_rank"))
                        pool_seen.add(f.path)
                        injected_paths.append(f.path)
                        surf = classify_surface(f.path)
                        surface_pools.setdefault(surf, SurfacePool(surface=surf)).injected.append(f.path)

            # Order the pool by reranker score so deterministic rerank's top-5/top-10
            # co-rank checks stay score-aligned; injected floor seeds sit at the tail.
            pool_candidates.sort(key=lambda c: c.reranker_score, reverse=True)
            surface_counts: Dict[str, int] = {}
            for c in pool_candidates:
                surface = classify_surface(c.item_id)
                surface_counts[surface] = surface_counts.get(surface, 0) + 1
            recall = RecallStageResult(
                pool_paths=[c.item_id for c in pool_candidates],
                pool_limit=pool_limit,
                expected_surface=expected_surface,
                surface_pools=surface_pools,
                injected_paths=injected_paths,
                surface_counts=surface_counts,
            )
        else:
            # Output slots are a budget, not a recall budget. A five-file pack
            # still needs tail candidates for test co-ranking/noise replacement.
            pool_limit = max(file_limit, 30)
            pool_candidates = reranked[:pool_limit]
            recall.pool_paths = [c.item_id for c in pool_candidates]
            recall.pool_limit = pool_limit
        timing.recall_pool_ms = (time.perf_counter() - t_recall) * 1000
        return recall, pool_candidates
    async def _stage_v5_rerank(self, *, candidates_by_channel, pool_candidates, path_to_file, card_by_path, card_rerank, file_limit, repository_id, v2_rerank_off, reranked, task_description, task_type, repository_name, v6_enabled, expected_surface, recall, timing):
        t_v5 = time.perf_counter()
        vector_by_path = {c.item_id: c.raw_score for c in candidates_by_channel.get("vector", [])}
        lexical_by_path = {c.item_id: c.normalized_score for c in candidates_by_channel.get("lexical", [])}
        symbol_by_path = {c.item_id: c.normalized_score for c in candidates_by_channel.get("symbol", [])}
        graph_by_path = {c.item_id: c.raw_score for c in candidates_by_channel.get("graph", [])}
        rerank_pool = _build_rerank_pool(
            pool_candidates,
            path_to_file=path_to_file,
            vector_by_path=vector_by_path,
            lexical_by_path=lexical_by_path,
            symbol_by_path=symbol_by_path,
            graph_by_path=graph_by_path,
            card_by_path=card_by_path,
            card_rerank=card_rerank,
        )
        precision_k = min(10, file_limit) if file_limit >= 10 else file_limit
        commit_hash = ""
        if repository_id is not None:
            async with async_session_factory() as session:
                repo_row = (
                    await session.execute(select(Repository.last_indexed_commit).where(Repository.id == repository_id))
                ).scalar_one_or_none()
                commit_hash = repo_row or ""
        if v2_rerank_off:
            v5_result = RerankResult(
                top_paths=[c.item_id for c in reranked[:precision_k]],
                candidates=[],
                stage="v2_no_rerank",
                fallback_reason="rerank gated off (RETRIEVAL_V2_RERANK_ENABLED=false)",
            )
        else:
            v5_result = await rerank_top_k(
                rerank_pool,
                task_description=task_description,
                task_type=task_type,
                repo_hash=repository_name,
                top_k=precision_k,
                expected_surface=expected_surface,
                two_stage=v6_enabled and bool(settings.RETRIEVAL_V6_SEMANTIC_RERANK),
                commit_hash=commit_hash,
            )
        baseline_v5_top = v5_result.top_paths
        available = {c.item_id for c in reranked} | set(recall.pool_paths)
        baseline_v5_top = expand_protected_pairs(baseline_v5_top, available)[:precision_k]
        timing.v5_rerank_ms = (time.perf_counter() - t_v5) * 1000
        return precision_k, commit_hash, v5_result, baseline_v5_top, available, vector_by_path, lexical_by_path, symbol_by_path, graph_by_path
    async def _stage_baseline_selection(self, *, probe_list, v2_rerank_off, repository_id, path_to_file, hint_tokens, file_limit, candidates_by_channel, reranked, baseline_v5_top, precision_k, keywords_lower, task_type, task_description, timing):
        # Final selection retains qualified hints while promoting recall paths.
        t_budget = time.perf_counter()
        pinned: List[str] = []
        if probe_list and not v2_rerank_off:
            async with async_session_factory() as session:
                stmt_probe = select(File).where(File.path.in_(probe_list))
                if repository_id is not None:
                    stmt_probe = stmt_probe.where(File.repository_id == repository_id)
                for f in (await session.execute(stmt_probe)).scalars().all():
                    if not should_exclude_from_retrieval(f.path):
                        pinned.append(f.path)
                        path_to_file.setdefault(f.path, f)

        if not v2_rerank_off:
            pinned = _pin_qualified_path_hints(
                candidates_by_channel["hints"], list(hint_tokens), pinned, file_limit,
            )

        if v2_rerank_off:
            # Fusion order IS the selection: no probe pins, no channel promotions.
            baseline_selected = [c.item_id for c in reranked[:file_limit]]
        else:
            baseline_selected = _select_final_paths(
                reranked=reranked,
                v5_top=baseline_v5_top,
                file_limit=file_limit,
                precision_k=precision_k,
                candidates_by_channel=candidates_by_channel,
                keywords_lower=keywords_lower,
                task_type=task_type,
                task_description=task_description,
                pinned=pinned,
            )
        timing.budget_selection_ms = (time.perf_counter() - t_budget) * 1000
        return pinned, baseline_selected
    async def _stage_late_interaction(self, *, repository_id, deep_mode, task_description, pool_candidates, vector_result, reranked, path_to_file, vector_by_path, lexical_by_path, symbol_by_path, graph_by_path, card_by_path, card_rerank, available, precision_k, expected_surface, v6_enabled, commit_hash, task_type, repository_name, file_limit, candidates_by_channel, keywords_lower, pinned, baseline_selected, baseline_v5_top, v5_result, late_interaction_client, route, timing):
        late_debug: dict = {
            "status": "disabled",
            "shadow": bool(getattr(settings, "LATE_INTERACTION_SHADOW_ENABLED", False)),
            "rerank_enabled": bool(getattr(settings, "LATE_INTERACTION_RERANK_ENABLED", False)),
            "canary_percent": float(getattr(settings, "LATE_INTERACTION_CANARY_PERCENT", 0.0)),
            "applied": False,
        }
        selected = baseline_selected
        v5_top = baseline_v5_top
        v5_debug_result = v5_result
        counterfactual_state: dict = {}
        if repository_id is not None and (
            deep_mode or late_interaction_requested(repository_id, task_description)
        ):
            try:
                late_candidates = await _resolve_late_interaction_candidates(
                    repository_id=repository_id,
                    pool_paths=[candidate.item_id for candidate in pool_candidates],
                    dense_matches=vector_result.matches,
                )
            except Exception as exc:
                logger.warning(f"Late-interaction candidate resolution failed open: {exc}")
                late_candidates = []

            async def _build_file_counterfactual(remote_result) -> list[str]:
                path_scores: dict[str, float] = {}
                for score in remote_result.scores:
                    path_scores[score.path] = max(
                        path_scores.get(score.path, float("-inf")),
                        float(score.score),
                    )

                counter_by_path = {
                    candidate.item_id: copy.deepcopy(candidate)
                    for candidate in reranked
                }
                for candidate in pool_candidates:
                    counter_by_path.setdefault(candidate.item_id, copy.deepcopy(candidate))
                _apply_late_interaction_scores(list(counter_by_path.values()), path_scores)
                counter_reranked = sorted(
                    [counter_by_path[candidate.item_id] for candidate in reranked],
                    key=lambda candidate: candidate.reranker_score,
                    reverse=True,
                )
                counter_pool = sorted(
                    [counter_by_path[candidate.item_id] for candidate in pool_candidates],
                    key=lambda candidate: candidate.reranker_score,
                    reverse=True,
                )
                counter_rerank_pool = _build_rerank_pool(
                    counter_pool,
                    path_to_file=path_to_file,
                    vector_by_path=vector_by_path,
                    lexical_by_path=lexical_by_path,
                    symbol_by_path=symbol_by_path,
                    graph_by_path=graph_by_path,
                    card_by_path=card_by_path,
                    card_rerank=card_rerank,
                )
                counter_v5_result = await _rerank_late_counterfactual(
                    counter_rerank_pool,
                    task_description=task_description,
                    task_type=task_type,
                    repository_name=repository_name,
                    precision_k=precision_k,
                    expected_surface=expected_surface,
                    two_stage=v6_enabled and bool(settings.RETRIEVAL_V6_SEMANTIC_RERANK),
                    commit_hash=commit_hash,
                )
                counter_v5_top = expand_protected_pairs(
                    counter_v5_result.top_paths,
                    available,
                )[:precision_k]
                counter_selected = _select_final_paths(
                    reranked=counter_reranked,
                    v5_top=counter_v5_top,
                    file_limit=file_limit,
                    precision_k=precision_k,
                    candidates_by_channel=candidates_by_channel,
                    keywords_lower=keywords_lower,
                    task_type=task_type,
                    task_description=task_description,
                    pinned=pinned,
                )
                counterfactual_state.update(
                    {
                        "reranked": counter_reranked,
                        "v5_top": counter_v5_top,
                        "v5_result": counter_v5_result,
                    }
                )
                return counter_selected

            late_application = await apply_late_interaction(
                query=task_description,
                repository_id=repository_id,
                candidates=late_candidates,
                baseline_top_k=baseline_selected,
                build_counterfactual=_build_file_counterfactual,
                query_class=route.value,
                index_revision=commit_hash,
                mode="deep" if deep_mode else "default",
                client=late_interaction_client,
            )
            timing.late_interaction_ms = late_application.latency_ms
            late_debug = late_application.to_debug()
            if deep_mode and not late_application.applied:
                raise RuntimeError(
                    "deep LFM rerank failed closed: "
                    f"{late_application.status}:"
                    f"{late_application.reason or 'not_applied'}"
                )
            if counterfactual_state:
                late_debug["counterfactual_rerank"] = {
                    "cache_hit": False,
                    "used_llm": False,
                    "stage": counterfactual_state["v5_result"].stage,
                }
            if late_application.applied and counterfactual_state:
                selected = late_application.effective_top_k
                reranked = counterfactual_state["reranked"]
                v5_top = counterfactual_state["v5_top"]
                v5_debug_result = counterfactual_state["v5_result"]
        return selected, v5_top, v5_debug_result, reranked, late_debug
