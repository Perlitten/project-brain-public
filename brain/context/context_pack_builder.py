import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from loguru import logger
from sqlalchemy import func, or_, select

from brain.database.session import async_session_factory
from brain.database.models import File, Symbol, Rule, Decision, Learning, ContextPack, IndexingRun
from brain.config.settings import settings
from brain.config.paths import context_packs_dir
from brain.graph.graph_client import GraphClient
from brain.llm.router import TaskKind, get_model_router
from brain.search.filters import (
    apply_pack_slot_policy,
    enforce_precision_top_k,
    expand_protected_pairs,
    trim_ranked_files,
)
from brain.search.code_search import extract_keywords
from brain.search.path_hints import expand_keywords
from brain.database.repository_utils import get_repository_by_path
from brain.memory.repo_scope import normalize_repo_scope, repository_scope_clause


def _learning_scope_clause(repo_scope: Optional[str]):
    """Include global learnings plus learnings owned by one repository."""
    normalized_scope = normalize_repo_scope(repo_scope)
    return or_(
        Learning.repo_scope.is_(None),
        func.lower(func.trim(Learning.repo_scope)) == (normalized_scope or "").casefold(),
    )


def _selected_retrieval_paths(retrieval_result: Any, file_limit: int) -> List[str]:
    """Honor an explicit retrieval abstention instead of resurrecting noise.

    An empty selection normally means the legacy pipeline needs its historical
    reranked fallback.  v2/abstention marks the empty selection in debug, which
    must remain empty all the way to the context pack.
    """
    debug = getattr(retrieval_result, "debug", {}) or {}
    if debug.get("abstained"):
        return []
    selected = retrieval_result.selected_paths or [c.item_id for c in retrieval_result.reranked if c.included]
    return selected or [c.item_id for c in retrieval_result.reranked[:file_limit]]


def _protected_retrieval_paths(retrieval_result: Any, file_limit: int, available: set[str]) -> List[str]:
    """Protect final pipeline selection; keep pair expansion for legacy fallback.

    Final selection already applies recall promotion and precision policy.
    Re-expanding intermediate reranker paths can undo that selection within a
    small pack budget and replace a chosen source with an unselected test.
    """
    if (getattr(retrieval_result, "debug", {}) or {}).get("abstained"):
        return []
    if retrieval_result.selected_paths:
        return list(dict.fromkeys(retrieval_result.selected_paths))[:file_limit]
    fallback = retrieval_result.v5_top_paths or _selected_retrieval_paths(retrieval_result, file_limit)[:10]
    return expand_protected_pairs(fallback, available)[:10]


async def _load_active_learnings(
    repo_scope: Optional[str],
    query: Optional[str] = None,
    limit: int = 10,
) -> List[Learning]:
    """Read active, non-expired learnings fresh for every context-pack build.

    Uses this module's async_session_factory (same seam as normative memory)
    so tests can mock all pack-time DB access in one place.

    When ``query`` is given, learnings are ranked by embedding cosine
    similarity to the query (most relevant first); otherwise by confidence.
    """
    now = datetime.now(timezone.utc)
    async with async_session_factory() as session:
        result = await session.execute(
            select(Learning).where(
                Learning.status == "active",
                or_(Learning.valid_until.is_(None), Learning.valid_until > now),
                _learning_scope_clause(repo_scope),
            ).order_by(Learning.confidence.desc(), Learning.id.desc())
        )
        learnings = list(result.scalars().all())

    if query and len(learnings) > 1:
        try:
            from brain.llm import get_embedding_provider

            embedder = get_embedding_provider()
            current_model = getattr(embedder, "model", None) or getattr(
                embedder, "provider", "unknown"
            )
            query_vec = await embedder.embed(query)
            scored: List[tuple] = []
            missing: List = []
            for lrng in learnings:
                vec = getattr(lrng, "embedding", None)
                if vec and getattr(lrng, "embedding_model", None) == current_model and len(vec) == len(query_vec):
                    scored.append((lrng, _cosine_similarity(query_vec, list(vec))))
                else:
                    missing.append(lrng)
            if missing:
                batch = await embedder.embed_batch([m.statement for m in missing])
                for lrng, vec in zip(missing, batch):
                    scored.append((lrng, _cosine_similarity(query_vec, vec)))
                    # Store for the dedup pass below (avoids re-embedding).
                    try:
                        lrng.embedding = vec
                    except Exception:
                        pass
            # Rank by combined score: cosine similarity is primary, but
            # recency and confidence break ties between learnings on the
            # same topic. Without this, a superseded learning (e.g. "use
            # LIFO", conf 0.6) ranks alongside its replacement ("use FIFO",
            # conf 0.95) because both match the query equally well —
            # and the stale one can surface in learnings_used.
            # Weights: similarity dominates; recency/confidence decide
            # near-ties (e.g. sim 0.91 vs 0.90 with conf 0.6 vs 0.95).
            now_ts = now.timestamp()
            # Normalize recency across the candidate set (0=oldest, 1=newest).
            created = [getattr(lrng, "created_at", None) for lrng, _ in scored]
            ts_list = [
                c.timestamp() if c is not None else now_ts
                for c in created
            ]
            t_min, t_max = min(ts_list), max(ts_list)
            t_span = t_max - t_min if t_max > t_min else 1.0
            ranked = []
            for (lrng, sim), ts in zip(scored, ts_list):
                recency = (ts - t_min) / t_span
                conf = float(getattr(lrng, "confidence", 0.5) or 0.5)
                conf = max(0.0, min(1.0, conf))
                # Combined: similarity is the main signal; recency and
                # confidence each contribute up to ~0.1 to break ties.
                combined = sim + 0.1 * recency + 0.1 * conf
                ranked.append((combined, lrng))
            ranked.sort(key=lambda pair: pair[0], reverse=True)
            # Topic dedup: group learnings by inter-similarity (>= 0.8 = same
            # topic). Within each group keep only the NEWEST (by created_at,
            # then confidence) — this suppresses superseded learnings like
            # "use LIFO" when "use FIFO" exists. Then rank groups by the best
            # query-similarity in the group.
            #
            # This fixes the case where an older learning has slightly higher
            # query cosine similarity than its newer replacement: pure cosine
            # ranking surfaces the stale one.
            groups: List[List] = []  # each group: list of (lrng, sim, ts, conf)
            for (lrng, sim), ts in zip(scored, ts_list):
                vec = getattr(lrng, "embedding", None)
                conf = float(getattr(lrng, "confidence", 0.5) or 0.5)
                conf = max(0.0, min(1.0, conf))
                placed = False
                if vec is not None:
                    vec_list = list(vec)
                    for group in groups:
                        rep_vec = getattr(group[0][0], "embedding", None)
                        if rep_vec is None:
                            continue
                        try:
                            gsim = _cosine_similarity(vec_list, list(rep_vec))
                        except Exception:
                            continue
                        # Same topic if high cosine similarity, OR moderate
                        # similarity with strong keyword overlap (handles
                        # paraphrases like "systemctl restart" vs "brain-env.sh
                        # wrapper" for the same "restart Brain API" topic).
                        same_topic = gsim >= 0.8
                        if not same_topic and gsim >= 0.65:
                            kw1 = _keywords(str(getattr(lrng, "statement", "")))
                            kw2 = _keywords(str(getattr(group[0][0], "statement", "")))
                            if len(kw1 & kw2) >= 3:
                                same_topic = True
                        if same_topic:
                            group.append((lrng, sim, ts, conf))
                            placed = True
                            break
                if not placed:
                    groups.append([(lrng, sim, ts, conf)])
            # Within each group: newest first, then highest confidence.
            # Across groups: rank by max query-similarity in the group.
            winners = []
            for group in groups:
                group.sort(key=lambda t: (t[2], t[3]), reverse=True)
                best_sim = max(t[1] for t in group)
                winners.append((best_sim, group[0][0]))
            winners.sort(key=lambda p: p[0], reverse=True)
            learnings = [lrng for _, lrng in winners]
        except Exception as exc:
            logger.warning(f"Learning rerank by query failed, using confidence order: {exc}")
    return learnings[:limit]


def _cosine_similarity(a: List[float], b: List[float]) -> float:
    denom = (sum(x * x for x in a) ** 0.5) * (sum(x * x for x in b) ** 0.5)
    if denom == 0:
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / denom


def _keywords(text: str) -> set:
    """Extract significant keywords for topic-overlap detection."""
    import re
    words = re.findall(r"\b[a-z]{3,}\b", text.lower())
    stop = {
        "the", "with", "via", "and", "for", "are", "was", "were", "been",
        "probe", "benchmark", "this", "that", "from", "into", "over",
    }
    return {w for w in words if w not in stop}


async def _latest_index_revision(repository_id: Optional[int]) -> Optional[Tuple[int, str]]:
    """Only a completed latest run can identify stable indexed evidence."""
    if repository_id is None:
        return None
    async with async_session_factory() as session:
        row = (
            await session.execute(
                select(IndexingRun.id, IndexingRun.commit_hash, IndexingRun.status)
                .where(IndexingRun.repository_id == repository_id)
                .order_by(IndexingRun.id.desc())
                .limit(1)
            )
        ).one_or_none()
    if row is None or row.status != "completed" or not row.commit_hash:
        return None
    return row.id, row.commit_hash


def _decision_scope_clause(repo_scope: Optional[str]):
    """Include global decisions plus decisions owned by this repository."""
    return repository_scope_clause(Decision, repo_scope)


def _rule_scope_clause(repo_scope: Optional[str]):
    """Include global rules plus rules owned by this repository."""
    return repository_scope_clause(Rule, repo_scope)


async def _load_active_normative_memory(
    repo_scope: Optional[str],
) -> Tuple[List[Rule], List[Decision]]:
    """Read rules and scoped decisions fresh for every context-pack build."""
    async with async_session_factory() as session:
        rules_result = await session.execute(
            select(Rule).where(
                Rule.status == "active",
                _rule_scope_clause(repo_scope),
            )
        )
        decisions_result = await session.execute(
            select(Decision).where(
                Decision.status == "active",
                _decision_scope_clause(repo_scope),
            )
        )
        return (
            list(rules_result.scalars().all()),
            list(decisions_result.scalars().all()),
        )


class StableMemoryCache:
    """CAG-style cache for repository files and symbols."""

    def __init__(self):
        # Last observed normative snapshot for dashboard counts only. Context
        # packs never read rules or decisions from these fields.
        self.rules: List[Rule] = []
        self.decisions: List[Decision] = []
        self.files: List[File] = []
        self.symbols: List[Symbol] = []
        self.initialized = False
        self.repository_id: Optional[int] = None
        self.repository_scope: Optional[str] = None

    async def initialize(
        self,
        repository_id: Optional[int] = None,
        repository_scope: Optional[str] = None,
    ):
        normalized_scope = normalize_repo_scope(repository_scope)
        if self.initialized and self.repository_id == repository_id and self.repository_scope == normalized_scope:
            return
        self.repository_id = repository_id
        self.repository_scope = normalized_scope
        try:
            async with async_session_factory() as session:
                # Load files
                stmt_files = select(File)
                if repository_id is not None:
                    stmt_files = stmt_files.where(File.repository_id == repository_id)
                res_files = await session.execute(stmt_files)
                self.files = list(res_files.scalars().all())

                # Load symbols
                stmt_symbols = select(Symbol).join(File, Symbol.file_id == File.id)
                if repository_id is not None:
                    stmt_symbols = stmt_symbols.where(File.repository_id == repository_id)
                res_symbols = await session.execute(stmt_symbols)
                self.symbols = list(res_symbols.scalars().all())

            self.initialized = True
            logger.info("StableMemoryCache: Loaded stable memory cache successfully.")
        except Exception as e:
            logger.error(f"StableMemoryCache: Failed to initialize cache: {e}")
            # Reset to a clean empty state + force re-init next call, so a caller
            # never reads a half-populated cache or another repo's stale rows.
            self.rules, self.decisions, self.files, self.symbols = [], [], [], []
            self.initialized = False
            self.repository_scope = None


class RetrievalCritic:
    """Evaluates the completeness and confidence of the retrieved context pack."""

    _STOPWORDS = {
        "the",
        "and",
        "for",
        "that",
        "this",
        "with",
        "from",
        "when",
        "where",
        "change",
        "update",
        "fix",
        "find",
        "add",
        "across",
        "into",
        "about",
    }

    @classmethod
    def _extract_target_terms(cls, task_description: str) -> List[str]:
        """Extract module/surface terms hinted by prepositions in the task text."""
        task_desc_lower = task_description.lower()
        terms: List[str] = []
        for match in re.finditer(r"(?:across|in|for|within|on)\s+([^.!?\n]+)", task_desc_lower):
            segment = match.group(1)
            for word in re.findall(r"\b[a-z]{4,}\b", segment):
                if word not in cls._STOPWORDS and word not in terms:
                    terms.append(word)
        return terms[:6]

    @staticmethod
    def critique(
        task_description: str, files: List[File], rules: List[Rule], decisions: List[Decision]
    ) -> Dict[str, Any]:
        warnings = []
        missing = []

        target_terms = RetrievalCritic._extract_target_terms(task_description)
        for term in target_terms:
            has_term_file = any(term in (f.path.lower() or "") or term in ((f.summary or "").lower()) for f in files)
            if not has_term_file:
                missing.append(f"{term} files")
                warnings.append(f"Task references '{term}', but no relevant files were found.")

        # 2. Test coverage validation
        has_tests = any("test" in (f.path.lower() or "") or "spec" in (f.path.lower() or "") for f in files)
        if not has_tests:
            missing.append("tests")
            warnings.append("No test files included in the retrieved context.")

        # 3. API Contract validation
        has_contracts = any(
            any(kw in (f.path.lower() or "") for kw in ["model", "schema", "dto", "contract"]) for f in files
        )
        if not has_contracts:
            missing.append("API contracts/schemas")
            warnings.append("No database models or API contract files included.")

        # 4. Status determination
        if not files:
            status = "LOW_CONFIDENCE"
            warnings.append("No relevant files retrieved.")
        elif missing:
            status = "PARTIAL_CONTEXT"
        else:
            status = "COMPLETE"

        return {"status": status, "missing": missing, "warnings": warnings}


class ContextPackBuilder:
    """Builds technical context packs using Hybrid Context Engine v2."""

    # Global static cache instance
    _cache = StableMemoryCache()

    def __init__(self):
        self.router = get_model_router()

    async def _classify_task(self, task_description: str) -> Tuple[str, List[str], List[str], List[str]]:
        """Classify task and extract keywords — deterministic by default."""
        lower = task_description.lower()
        task_type = "other"
        if any(token in lower for token in ("bug", "fix", "issue", "repair", "stop", "miscalculation")):
            task_type = "bugfix"
        elif "refactor" in lower or "rename" in lower:
            task_type = "refactor"
        elif any(token in lower for token in ("design", "ui", "style", "branding", "template", "theme", "badge")):
            task_type = "design_change"
        elif any(token in lower for token in ("migration", "table", "foreign key", "backfill", "deck_id", "database")):
            task_type = "database"
        elif any(token in lower for token in ("openapi", "dto", "schema", "contract", "payload", "breaking change")):
            task_type = "api_contract"
        elif any(token in lower for token in ("nginx", "docker", "extension", "across", "coordinate", "compose")):
            task_type = "cross_surface"
        elif any(
            token in lower for token in ("analytics", "event", "metric", "histogram", "dashboard", "override rate")
        ):
            task_type = "analytics"
        elif any(token in lower for token in ("adr", "boundary", "enforce", "policy", "colocated", "ownership")):
            task_type = "architecture_rule"
        elif any(token in lower for token in ("deprecated", "legacy", "remove module", "adapter")):
            task_type = "deletion_rename"
        elif any(token in lower for token in ("feat", "add", "wire", "emit", "align", "expose", "cli")):
            task_type = "feature"

        words = extract_keywords(task_description)
        stopwords = {
            "the",
            "and",
            "for",
            "that",
            "this",
            "with",
            "from",
            "should",
            "would",
            "could",
            "about",
            "when",
            "after",
            "before",
            "into",
            "across",
            "direct",
            "access",
        }
        keywords = list(dict.fromkeys(w for w in words if w not in stopwords))
        risks = ["Unintended side effects on related modules", "Potential regression in existing functionality"]
        features = ["Core"]

        if not settings.RETRIEVAL_USE_LLM_CLASSIFICATION:
            return task_type, keywords, risks, features

        prompt = (
            f"Analyze the following task description for a software project:\n"
            f'"""\n{task_description}\n"""\n\n'
            f"Classify task_type (bugfix|feature|refactor|design_change|other), "
            f"keywords, risks, features. Respond with JSON only."
        )
        try:
            response = await self.router.llm(TaskKind.CLASSIFICATION).generate(
                prompt=prompt,
                system_instruction="Return valid JSON with keys task_type, keywords, risks, features.",
            )
            cleaned = response.strip()
            if cleaned.startswith("```"):
                cleaned = re.sub(r"^```(?:json)?\n", "", cleaned)
                cleaned = re.sub(r"\n```$", "", cleaned)
            data = json.loads(cleaned.strip())
            return (
                data.get("task_type", task_type),
                data.get("keywords", keywords) or keywords,
                data.get("risks", risks),
                data.get("features", features),
            )
        except Exception as exc:
            logger.warning(f"LLM classification failed, using heuristics: {exc}")
            return task_type, keywords, risks, features

    async def build_context_pack(
        self,
        task_description: str,
        repo_path: Path,
        budget: str = "standard",
        debug_retrieval: bool = False,
        retrieval_mode: str = "fast",
        late_interaction_client=None,
    ) -> dict:
        """Classifies the task and retrieves context using Hybrid Context Engine v2."""
        logger.info(f"ContextPackBuilder: Building context pack with budget={budget}...")

        repo_record = await get_repository_by_path(repo_path)
        repository_id = repo_record.id if repo_record else None
        index_revision = await _latest_index_revision(repository_id)
        repository_name = repo_record.name if repo_record else repo_path.resolve().name
        repository_scope = normalize_repo_scope(repo_record.path if repo_record else repo_path.resolve().as_posix())
        graph_client = GraphClient(repository_id=repository_id) if repository_id is not None else None
        if repository_id is None:
            logger.warning(
                f"ContextPackBuilder: Repository '{repo_path}' is not indexed; "
                "retrieval may include stale cross-repo data."
            )

        # 1. Initialize CAG-style Cache
        await self._cache.initialize(
            repository_id=repository_id,
            repository_scope=repository_scope,
        )

        # 2. Token Budget Config
        budget = budget.lower()
        if budget == "small":
            file_limit = 3
        elif budget == "deep":
            file_limit = 30
        else:
            file_limit = 5

        # 3. Classify task type and extract keywords/entities
        task_type, keywords, risks, features = await self._classify_task(task_description)
        keywords_lower = expand_keywords(keywords, task_description)

        # 4–5. Hybrid retrieval pipeline (single orchestration path)
        from brain.retrieval.pipeline import HybridRetrievalPipeline

        retrieval_pipeline = HybridRetrievalPipeline(cache=self._cache)
        retrieval_result = await retrieval_pipeline.run(
            task_description=task_description,
            task_type=task_type,
            keywords=keywords,
            repository_id=repository_id,
            repository_name=repository_name,
            file_limit=file_limit,
            retrieval_mode=retrieval_mode,
            late_interaction_client=late_interaction_client,
        )
        vector_status = retrieval_result.vector_status
        channels = retrieval_result.candidates_by_channel

        vector_scores_by_path = {c.item_id: c.raw_score for c in channels.get("vector", [])}
        graph_files = [(c.item_id, int(c.raw_score)) for c in channels.get("graph", [])]
        unique_vector_files = [c.item_id for c in channels.get("vector", [])]
        hint_paths = {c.item_id for c in channels.get("hints", [])}
        hint_files: List[File] = []
        if hint_paths and self._cache.initialized:
            hint_files = [f for f in self._cache.files if f.path in hint_paths]
        if hint_paths and not hint_files:
            async with async_session_factory() as session:
                stmt_hints = select(File).where(File.path.in_(list(hint_paths)))
                if repository_id is not None:
                    stmt_hints = stmt_hints.where(File.repository_id == repository_id)
                hint_files = list((await session.execute(stmt_hints)).scalars().all())

        abstained = bool((getattr(retrieval_result, "debug", {}) or {}).get("abstained"))
        scored_files: List[Tuple[float, str, Dict[str, Any]]] = []
        available_paths = set(retrieval_result.selected_paths or [])
        available_paths.update(c.item_id for c in retrieval_result.reranked)
        pipeline_top = _protected_retrieval_paths(retrieval_result, file_limit, available_paths)
        protected_paths = set(pipeline_top)
        selected_paths = _selected_retrieval_paths(retrieval_result, file_limit)

        score_by_path = {c.item_id: c.reranker_score for c in retrieval_result.reranked}
        v5_explanations = (retrieval_result.v5_rerank_debug or {}).get("explanations", {})
        for path in selected_paths:
            ranks = {}
            for cand in retrieval_result.reranked:
                if cand.item_id == path:
                    ranks = cand.metadata.get("ranks", {})
                    break
            trace = {
                "lexical_match": "lexical" in ranks,
                "path_hint_match": "hints" in ranks,
                "vector_similarity": vector_scores_by_path.get(path, 0.0),
                "graph_relation": "graph" in ranks,
                "cache_match": "memory" in ranks,
                "final_score": round(score_by_path.get(path, 1.0), 5),
                "v5_explanation": v5_explanations.get(path, ""),
                "protected": path in protected_paths,
            }
            scored_files.append((score_by_path.get(path, 1.0), path, trace))

        scored_files.sort(key=lambda item: item[0], reverse=True)

        # Preserve final pipeline choices inside the requested pack budget.
        v5_ordered = []
        for p in pipeline_top:
            for entry in scored_files:
                if entry[1] == p:
                    v5_ordered.append(entry)
                    break
        remaining_entries = [e for e in scored_files if e[1] not in protected_paths]
        scored_files = v5_ordered + remaining_entries

        capped_files, pack_exclusions = apply_pack_slot_policy(
            scored_files,
            file_limit,
            protected_paths,
            task_type,
            task_description,
            budget,
        )
        for path, reason in pack_exclusions.items():
            logger.debug(f"ContextPack: excluded {path} — {reason}")

        top_files_data = trim_ranked_files(
            capped_files or scored_files,
            file_limit,
            min_keep=min(10, file_limit),
            aggressive=budget != "deep",
        )
        ordered_paths = [path for _, path, _ in top_files_data]
        # Ensure final pipeline paths occupy the protected pack slots.
        head = [p for p in pipeline_top if p in ordered_paths or p in protected_paths]
        for p in protected_paths:
            if p not in head:
                head.append(p)
        tail = [p for p in ordered_paths if p not in head]
        ordered_paths = (head + tail)[:file_limit]
        ordered_paths = enforce_precision_top_k(
            ordered_paths,
            task_type,
            task_description,
            k=min(10, file_limit),
            priority_paths=list(pipeline_top),
        )
        head = [p for p in pipeline_top if p in ordered_paths]
        tail = [p for p in ordered_paths if p not in head]
        ordered_paths = (head + tail)[:file_limit]
        trace_by_path = {path: trace for _, path, trace in (capped_files or scored_files)}
        top_files_data = []
        for path in ordered_paths:
            trace = dict(trace_by_path.get(path, {}))
            if "final_score" not in trace:
                trace["final_score"] = round(score_by_path.get(path, 1.0), 5)
            if path in pack_exclusions:
                trace["pack_exclusion_reason"] = pack_exclusions[path]
            top_files_data.append((score_by_path.get(path, trace.get("final_score", 1.0)), path, trace))
        top_files_data = top_files_data[:file_limit]
        promoted_paths = {path for _, path, _ in top_files_data}
        for hint_file in ([] if abstained else hint_files[:2]):
            if hint_file.path in promoted_paths:
                continue
            if len(top_files_data) >= file_limit:
                break
            hint_entry = (
                top_files_data[0][0] if top_files_data else 1.0,
                hint_file.path,
                {
                    "lexical_match": False,
                    "path_hint_match": True,
                    "vector_similarity": 0.0,
                    "graph_relation": False,
                    "cache_match": False,
                    "final_score": top_files_data[0][0] if top_files_data else 1.0,
                },
            )
            top_files_data.append(hint_entry)
            promoted_paths.add(hint_file.path)

        scored_symbols: List[Tuple[float, str, Dict[str, Any]]] = []
        for cand in ([] if abstained else channels.get("symbol", [])[:file_limit]):
            scored_symbols.append(
                (
                    cand.normalized_score,
                    cand.item_id,
                    {
                        "lexical_match": True,
                        "graph_relation": False,
                        "cache_match": False,
                        "final_score": round(cand.normalized_score, 5),
                    },
                )
            )
        scored_symbols.sort(key=lambda x: x[0], reverse=True)
        top_symbols_data = scored_symbols[:file_limit]

        # Debug helpers for retrieval breakdown
        lexical_files: List[File] = []
        lexical_symbols: List[Symbol] = []
        cache_files: List[File] = []
        if self._cache.initialized:
            lex_paths = {c.item_id for c in channels.get("lexical", [])}
            lexical_files = [f for f in self._cache.files if f.path in lex_paths]
            sym_names = {c.item_id for c in channels.get("symbol", [])}
            lexical_symbols = [s for s in self._cache.symbols if s.name in sym_names]
            mem_paths = {c.item_id for c in channels.get("memory", [])}
            cache_files = [f for f in self._cache.files if f.path in mem_paths]

        # Fetch full SQLAlchemy model records for top files and symbols using cache/bulk DB
        final_files: List[Tuple[File, Dict[str, Any]]] = []
        top_paths = [path for score, path, trace in top_files_data]
        files_by_path = {}
        if self._cache.initialized:
            for f in self._cache.files:
                if f.path in top_paths:
                    files_by_path[f.path] = f

        missing_paths = set(top_paths) - set(files_by_path.keys())
        if missing_paths:
            async with async_session_factory() as session:
                stmt_f = select(File).where(File.path.in_(list(missing_paths)))
                res_f = await session.execute(stmt_f)
                for db_file in res_f.scalars().all():
                    files_by_path[db_file.path] = db_file

        for score, path, trace in top_files_data:
            f_rec = files_by_path.get(path)
            if f_rec:
                final_files.append((f_rec, trace))

        final_symbols: List[Tuple[Symbol, Dict[str, Any]]] = []
        top_names = [name for score, name, trace in top_symbols_data]
        symbols_by_name: Dict[str, Symbol] = {}
        if self._cache.initialized:
            for s in self._cache.symbols:
                if s.name in top_names:
                    symbols_by_name[s.name] = s

        missing_names = set(top_names) - set(symbols_by_name.keys())
        if missing_names:
            async with async_session_factory() as session:
                stmt_s = select(Symbol).where(Symbol.name.in_(list(missing_names)))
                res_s = await session.execute(stmt_s)
                for db_symbol in res_s.scalars().all():
                    if db_symbol.name not in symbols_by_name:
                        symbols_by_name[db_symbol.name] = db_symbol

        for score, name, trace in top_symbols_data:
            s_rec = symbols_by_name.get(name)
            if s_rec:
                final_symbols.append((s_rec, trace))

        # 6. Rules & Decisions Retrieval. Normative memory is deliberately not
        # process-cached: a new decision or revocation must affect the next pack.
        active_rules, active_decisions = await _load_active_normative_memory(repository_scope)
        self._cache.rules = active_rules
        self._cache.decisions = active_decisions

        # 6b. Consolidated learnings (L3 semantic memory). Same freshness
        # guarantee as normative memory: always read live from Postgres.
        active_learnings = await _load_active_learnings(repository_scope)

        # 7. Critique the Context Pack
        critic_res = RetrievalCritic.critique(
            task_description=task_description,
            files=[f for f, t in final_files],
            rules=active_rules,
            decisions=active_decisions,
        )

        if debug_retrieval:
            print("\n=== RETRIEVER DEBUG BREAKDOWN ===")

            print("\nLexicalRetriever:")
            if lexical_files:
                for f in lexical_files[:5]:
                    print(f"  - File: {f.path}")
            else:
                print("  EMPTY")

            print("\nSymbolRetriever:")
            if lexical_symbols:
                for s in lexical_symbols[:5]:
                    print(f"  - Symbol: {s.name} ({s.kind})")
            else:
                print("  EMPTY")

            print("\nVectorRetriever:")
            if unique_vector_files:
                for path in unique_vector_files[:5]:
                    print(f"  - File: {path}")
            else:
                print("  EMPTY")

            print("\nGraphRetriever:")
            if graph_files:
                for path, count in graph_files[:5]:
                    print(f"  - File: {path} (visit count: {count})")
            else:
                print("  EMPTY")

            print("\nMemoryRetriever:")
            if active_rules or active_decisions:
                for r in active_rules[:3]:
                    print(f"  - Rule: {r.id} - {r.description}")
                for d in active_decisions[:3]:
                    print(f"  - Decision: {d.title} - {d.description}")
            else:
                print("  EMPTY")

            print("\nCacheRetriever:")
            if cache_files:
                for f in cache_files[:5]:
                    print(f"  - File: {f.path}")
            else:
                print("  EMPTY")

            print("\nRRF merged ranking:")
            if top_files_data:
                for idx, (score, path, trace) in enumerate(top_files_data[:10], 1):
                    print(f"  {idx}. {path} (Score: {score:.5f})")
            else:
                print("  EMPTY")

            print("\nRetrievalCritic:")
            print(f"  Status: {critic_res['status']}")
            print(f"  Warnings: {critic_res['warnings']}")
            print(f"  Missing: {critic_res['missing']}")
            print("=================================\n")

        # 8. Generate plan and checklist using LLM
        context_files_str = ", ".join([f.path for f, t in final_files])
        context_symbols_str = ", ".join([f"{s.name} ({s.kind})" for s, t in final_symbols])
        context_summary = (
            f"Task Description: {task_description}\n"
            f"Task Type: {task_type}\n"
            f"Extracted Keywords: {', '.join(keywords)}\n"
            f"Relevant Features: {', '.join(features)}\n"
            f"Relevant Files: {context_files_str}\n"
            f"Relevant Symbols: {context_symbols_str}\n"
            f"Active Rules: {', '.join([r.id for r in active_rules])}\n"
            f"Active Decisions: {', '.join([d.title for d in active_decisions])}\n"
        )

        plan_prompt = (
            f"Given the following task context:\n"
            f'"""\n{context_summary}\n"""\n\n'
            f"Please generate:\n"
            f"1. A detailed technical 'Suggested Implementation Plan' (step-by-step description of what needs to be changed and where).\n"
            f"2. A 'Verification Checklist' containing specific checklist items (e.g. manual verification steps, tests to write or run) to ensure the changes work correctly and do not introduce regressions.\n"
            f"3. A list of 'Technical Risks' or side-effects.\n\n"
            f"Respond ONLY with a JSON object containing keys: 'plan' (string with markdown bullet points), 'checklist' (list of strings for checklist items), and 'risks' (list of strings for technical risks)."
        )

        plan_content = ""
        checklist = []

        if settings.CONTEXT_PACK_SKIP_PLAN_LLM:
            plan_content = (
                "1. Review retrieved files and symbols for the task surface.\n"
                "2. Apply targeted changes in the listed modules.\n"
                "3. Run focused tests for the affected area."
            )
            checklist = [
                "Verify compilation/build succeeds.",
                "Run tests covering the modified modules.",
                "Manually validate the reported behavior.",
            ]
        else:
            try:
                response = await self.router.llm(TaskKind.SYNTHESIS).generate(
                    prompt=plan_prompt,
                    system_instruction="You are an expert software architect. Respond with valid JSON only.",
                )
                cleaned = response.strip()
                if cleaned.startswith("```"):
                    cleaned = re.sub(r"^```(?:json)?\n", "", cleaned)
                    cleaned = re.sub(r"\n```$", "", cleaned)
                plan_data = json.loads(cleaned.strip())
                plan_content = plan_data.get("plan", "No plan generated.")
                checklist = plan_data.get("checklist", [])
                if plan_data.get("risks"):
                    risks = plan_data.get("risks")
            except Exception as e:
                logger.warning(f"Failed to generate plan using LLM: {e}")
                plan_content = (
                    "1. Review the list of relevant files and symbols.\n"
                    "2. Apply target modifications to address the request.\n"
                    "3. Verify changes with manual and automated tests."
                )
                checklist = [
                    "Verify that compilation succeeds.",
                    "Ensure existing tests pass.",
                    "Write unit tests for the modified functions.",
                ]

        # 9. Format Markdown Report with RetrievalTraces
        repo_posix = repo_path.resolve().as_posix()
        files_md = ""
        if final_files:
            for f, t in final_files:
                trace_str = (
                    f"RRF Score: {t.get('final_score', 0)} "
                    f"[Lexical: {t.get('lexical_match', False)}, Vector: {t.get('vector_similarity', 0):.2f}, "
                    f"Graph: {t.get('graph_relation', False)}, Cache: {t.get('cache_match', False)}]"
                )
                files_md += f"- [{f.path}](file:///{repo_posix}/{f.path}) - {f.summary or 'No summary'}\n  * *Trace*: {trace_str}\n"
        else:
            files_md = "*No files found matching the task keywords.*\n"

        symbols_md = ""
        if final_symbols:
            for s, t in final_symbols:
                trace_str = (
                    f"RRF Score: {t['final_score']} "
                    f"[Lexical: {t['lexical_match']}, Graph: {t['graph_relation']}, Cache: {t['cache_match']}]"
                )
                symbols_md += f"- `{s.name}` ({s.kind}) - {s.summary or 'No summary'}\n  * *Trace*: {trace_str}\n"
        else:
            symbols_md = "*No symbols found matching the task keywords.*\n"

        # Format Graph neighborhood relationships from cypher results
        graph_md = ""
        graph_seen_edges = set()
        if keywords_lower and graph_client is not None:
            for kw in keywords_lower[:5]:
                try:
                    cypher = (
                        "MATCH (repo:Repository) "
                        "WHERE repo.repository_id = $repository_id "
                        "MATCH (repo)-[:CONTAINS*0..6]->(n) "
                        "WHERE n.repository_id = $repository_id "
                        "AND (toLower(n.name) CONTAINS toLower($kw) "
                        "OR (n.path IS NOT NULL AND toLower(n.path) CONTAINS toLower($kw))) "
                        "MATCH (n)-[r]-(m) "
                        "WHERE m.repository_id = $repository_id "
                        "RETURN n, r, m LIMIT 15"
                    )
                    async with graph_client.driver.session() as session_neo4j:
                        result = await session_neo4j.run(
                            cypher,
                            kw=kw,
                            repository_id=repository_id,
                        )
                        async for record in result:
                            n_node = record["n"]
                            r_rel = record["r"]
                            m_node = record["m"]
                            n_label = list(n_node.labels)[0] if n_node.labels else "Entity"
                            m_label = list(m_node.labels)[0] if m_node.labels else "Entity"
                            n_name = n_node.get("name") or n_node.get("path")
                            m_name = m_node.get("name") or m_node.get("path")
                            edge_key = (n_name, r_rel.type, m_name)
                            if edge_key not in graph_seen_edges:
                                graph_seen_edges.add(edge_key)
                                graph_md += f"- ({n_label}: `{n_name}`) -[{r_rel.type}]-> ({m_label}: `{m_name}`)\n"
                except Exception:
                    pass
        if not graph_md:
            graph_md = "*No relevant graph neighbors found.*\n"

        rules_md = ""
        if active_rules:
            for r in active_rules:
                rules_md += f"- **{r.id}** ({r.severity or 'medium'} severity): {r.description or 'No description'}\n"
        else:
            rules_md = "*No active rules defined.*\n"

        decisions_md = ""
        if active_decisions:
            for d in active_decisions:
                decisions_md += f"- **{d.title}** ({d.status}): {d.description or 'No description'}\n"
        else:
            decisions_md = "*No active decisions recorded.*\n"

        learnings_md = ""
        if active_learnings:
            for lrng in active_learnings[:10]:
                learnings_md += f"- {lrng.statement} _(confidence {lrng.confidence:.2f})_\n"
        else:
            learnings_md = "*No consolidated learnings yet.*\n"

        risks_md = ""
        if risks:
            for risk in risks:
                risks_md += f"- {risk}\n"
        else:
            risks_md = "*No specific risks identified.*\n"

        checklist_md = ""
        if checklist:
            for item in checklist:
                checklist_md += f"- [ ] {item}\n"
        else:
            checklist_md = "- [ ] Verify that implementation changes are correct.\n"

        # Format warnings/critic status
        critic_status = critic_res["status"]
        critic_warnings_md = ""
        if critic_res["warnings"]:
            critic_warnings_md = "### Critic Warnings\n"
            for w in critic_res["warnings"]:
                critic_warnings_md += f"> [!WARNING]\n> {w}\n\n"

        title_snippet = re.sub(r"[^a-zA-Z0-9\s]", "", task_description[:50]).strip()
        markdown_content = f"""# Context Pack: {title_snippet}

## Critic Evaluation Status: **{critic_status}**
{critic_warnings_md}
## Task Details
- **Task Type**: {task_type}
- **Description**: {task_description}
- **Features**: {", ".join(features) if features else "None"}
- **Token Budget Mode**: {budget}

## Relevant Features & Files
### Files
{files_md}

### Symbols
{symbols_md}

## Graph Neighbors (Context)
{graph_md}

## Active Rules & Decisions
### Rules
{rules_md}

### Decisions
{decisions_md}

### Consolidated Learnings
{learnings_md}

## Known Risks
{risks_md}

## Suggested Implementation Plan
{plan_content}

## Verification Checklist
{checklist_md}
"""

        # 10. Persist the generated artifact outside the indexed repository.
        # Production repositories are intentionally mounted read-only; the
        # context-pack volume is the writable system-of-record for these files.
        output_dir = context_packs_dir()
        output_dir.mkdir(parents=True, exist_ok=True)

        slug = re.sub(r"[^a-zA-Z0-9]", "_", task_description[:40]).strip("_").lower()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        filename = f"context_pack_{timestamp}_{slug}.md"
        output_file_path = output_dir / filename

        with open(output_file_path, "w", encoding="utf-8") as fp:
            fp.write(markdown_content)

        # 11. Save ContextPack to database with provenance: the repository and
        # the index revision its evidence came from. Readiness scopes first-use
        # completion on these — a pack from another repository must not count.
        final_index_revision = await _latest_index_revision(repository_id)
        repo_commit = (
            index_revision[1]
            if index_revision is not None and index_revision == final_index_revision
            else None
        )
        async with async_session_factory() as session:
            cp_record = ContextPack(
                task_description=task_description,
                path=output_file_path.as_posix(),
                repository_id=repository_id,
                repo_commit=repo_commit,
            )
            session.add(cp_record)
            await session.commit()
            await session.refresh(cp_record)
            cp_id = cp_record.id

        logger.info(f"ContextPackBuilder: Saved context pack to {output_file_path} with status={critic_status}")

        return {
            "id": cp_id,
            "task_type": task_type,
            "keywords": keywords,
            "risks": risks,
            "features": features,
            "path": output_file_path.as_posix(),
            "critic_status": critic_status,
            "markdown_content": markdown_content,
            "retrieved_files": [{"path": f.path, "trace": t} for f, t in final_files],
            "retrieved_symbols": [{"name": s.name, "trace": t} for s, t in final_symbols],
            "vector_status": vector_status,
            "v5_rerank": retrieval_result.v5_rerank_debug,
            "recall": retrieval_result.recall_debug,
            "retrieval_debug": getattr(retrieval_result, "debug", {}) or {},
            "retrieval_mode": retrieval_mode,
        }
