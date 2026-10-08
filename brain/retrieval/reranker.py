"""Ranking v5 — rerank top-30 fused candidates to precision top-10."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from brain.config.settings import settings
from brain.search.filters import (
    is_extension_noise_path,
    is_extension_path,
    is_extension_tab_path,
    is_script_noise_path,
    is_test_path,
    paired_src_path,
    paired_test_path,
    surface_mismatch_penalty,
)
from brain.search.surfaces import classify_surface
from brain.search.task_intent import derive_task_intent


# Bump when scoring/selection logic changes so cached results are invalidated.
RERANKER_VERSION = "v6.p2.2"


@dataclass
class RerankCandidate:
    path: str
    reranker_score: float
    lexical_score: float = 0.0
    vector_score: float = 0.0
    graph_score: float = 0.0
    symbol_score: float = 0.0
    summary: str = ""
    symbol_names: List[str] = field(default_factory=list)
    card_score: float = 0.0
    v5_score: float = 0.0
    explanation: str = ""
    confidence: float = 0.0
    rank_before: int = 0
    rank_after: int = 0
    rank_delta: int = 0


@dataclass
class RerankResult:
    top_paths: List[str]
    candidates: List[RerankCandidate]
    cache_hit: bool = False
    cache_key: str = ""
    used_llm: bool = False
    fallback: bool = True
    latency_ms: float = 0.0
    estimated_cost_usd: float = 0.0
    position_changes: List[Dict[str, Any]] = field(default_factory=list)
    stage: str = "deterministic"
    reranker_version: str = RERANKER_VERSION
    excluded_relevant: List[Dict[str, Any]] = field(default_factory=list)
    fallback_reason: str = ""


def _normalize(path: str) -> str:
    return path.replace("\\", "/").lower()


def _src_stem(path: str) -> str:
    norm = _normalize(path)
    name = norm.rsplit("/", 1)[-1]
    base = name.rsplit(".", 1)[0]
    return base.replace("test_", "").replace("_test", "")


def _paired_test_for_src(src_path: str) -> Optional[str]:
    return paired_test_path(src_path)


def _paired_src_for_test(test_path: str) -> Optional[str]:
    return paired_src_path(test_path)


def _wants_doc_surface(task_description: str, task_type: str) -> bool:
    lower = task_description.lower()
    return task_type in {"docs", "architecture_rule", "feature"} or any(
        token in lower
        for token in (
            "readme", "document", "documentation", "golden", "guidance",
            "onboarding", "how to", "workflow", "pipeline readme",
        )
    )


def _is_strong_v5_promotion(explanation: str) -> bool:
    return any(
        token in explanation
        for token in (
            "test co-ranked",
            "pair-boost",
            "doc/README",
            "golden doc",
            "database task test",
            "tab↔core",
            "extension core",
            "engine test bundle",
        )
    )


def _cache_dir() -> Path:
    root = Path(__file__).resolve().parents[2]
    cache = root / ".cache" / "rerank"
    cache.mkdir(parents=True, exist_ok=True)
    return cache


def build_cache_key(
    repo_hash: str,
    task_description: str,
    candidate_paths: List[str],
) -> str:
    task_hash = hashlib.sha256(task_description.encode("utf-8")).hexdigest()[:16]
    pool_hash = hashlib.sha256("|".join(sorted(candidate_paths)).encode("utf-8")).hexdigest()[:16]
    return f"{repo_hash[:12]}_{task_hash}_{pool_hash}"


def _load_cache(cache_key: str) -> Optional[RerankResult]:
    path = _cache_dir() / f"{cache_key}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        candidates = [RerankCandidate(**c) for c in data.get("candidates", [])]
        return RerankResult(
            top_paths=data.get("top_paths", []),
            candidates=candidates,
            cache_hit=True,
            cache_key=cache_key,
            used_llm=data.get("used_llm", False),
            fallback=data.get("fallback", True),
            latency_ms=0.0,
            estimated_cost_usd=0.0,
            position_changes=data.get("position_changes", []),
            stage=data.get("stage", "deterministic"),
            reranker_version=data.get("reranker_version", RERANKER_VERSION),
            excluded_relevant=data.get("excluded_relevant", []),
            fallback_reason=data.get("fallback_reason", ""),
        )
    except Exception as exc:
        logger.debug(f"Rerank cache read failed: {exc}")
        return None


def _save_cache(cache_key: str, result: RerankResult) -> None:
    path = _cache_dir() / f"{cache_key}.json"
    payload = {
        "top_paths": result.top_paths,
        "candidates": [
            {
                "path": c.path,
                "reranker_score": c.reranker_score,
                "lexical_score": c.lexical_score,
                "vector_score": c.vector_score,
                "graph_score": c.graph_score,
                "symbol_score": c.symbol_score,
                "summary": c.summary,
                "symbol_names": c.symbol_names,
                "v5_score": c.v5_score,
                "explanation": c.explanation,
                "confidence": c.confidence,
                "rank_before": c.rank_before,
                "rank_after": c.rank_after,
                "rank_delta": c.rank_delta,
            }
            for c in result.candidates
        ],
        "used_llm": result.used_llm,
        "fallback": result.fallback,
        "position_changes": result.position_changes,
        "stage": result.stage,
        "reranker_version": result.reranker_version,
        "excluded_relevant": result.excluded_relevant,
        "fallback_reason": result.fallback_reason,
    }
    try:
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.debug(f"Rerank cache write failed: {exc}")


def _surface_wanted(surface: str, intent) -> bool:
    """Whether task intent explicitly wants candidates from a given surface."""
    return (
        (surface == "extension" and intent.wants_extension)
        or (surface == "scripts" and (intent.wants_scripts or intent.wants_cli))
        or (surface == "config" and (intent.wants_deploy or intent.wants_root_config))
        or (surface == "engine" and intent.wants_ml)
    )


def deterministic_rerank(
    pool: List[RerankCandidate],
    task_description: str,
    task_type: str,
    top_k: int = 10,
    expected_surface: str = "",
) -> RerankResult:
    """Deterministic rerank — test co-rank, doc boost, surface demotion.

    When ``expected_surface`` is set (v6) a structural surface-mismatch penalty
    demotes clearly off-surface noise; passing "" preserves the frozen v5 path.
    """
    intent = derive_task_intent(task_description, task_type=task_type)
    lower = task_description.lower()
    wants_doc = _wants_doc_surface(task_description, task_type)
    wants_engine = (
        task_type in {"bugfix", "architecture_rule"}
        and not intent.wants_extension
        and task_type not in {"database", "cross_surface", "design_change"}
    ) or (
        any(
            token in lower
            for token in ("parser", "ability", "solver")
        )
        and task_type == "bugfix"
    )

    path_to_idx = {c.path: i + 1 for i, c in enumerate(pool)}
    top5_paths = {c.path for c in pool[:5]}
    top10_paths = {c.path for c in pool[:10]}

    scored: List[Tuple[float, RerankCandidate, List[str]]] = []
    for cand in pool:
        norm = _normalize(cand.path)
        score = cand.reranker_score
        reasons: List[str] = []

        # Test co-rank: boost test when paired src is in top-5
        if is_test_path(cand.path):
            src_guess = _paired_src_for_test(cand.path)
            if src_guess and any(_normalize(p) == _normalize(src_guess) for p in top5_paths):
                score += 0.85
                reasons.append("test co-ranked with top-5 src file")
            for p in top10_paths:
                if p in top5_paths:
                    continue
                if _src_stem(p) == _src_stem(cand.path) and not is_test_path(p):
                    score += 0.58
                    reasons.append("test co-ranked with top-10 src file")
                    break
            for p in top5_paths:
                if _src_stem(p) == _src_stem(cand.path) and not is_test_path(p):
                    score += 0.72
                    reasons.append("pair-boost with top-5 src sibling")
                    break

        # Pair-boost: src file when its test is already in top-10
        if not is_test_path(cand.path):
            test_guess = _paired_test_for_src(cand.path)
            if test_guess and any(_normalize(p) == _normalize(test_guess) for p in top10_paths):
                score += 0.35
                reasons.append("src boosted because paired test in top-10")

        # README / doc intent
        if wants_doc and (norm.endswith("/readme.md") or norm.endswith("readme.md")):
            score += 0.55
            reasons.append("doc/README intent boost")
        if wants_doc and "/golden/" in norm and norm.endswith(".md"):
            score += 0.45
            reasons.append("golden doc path boost")

        # Extension tab ↔ core pattern (general, not task-specific)
        if norm.endswith("extension/src/options/core.ts") or norm.endswith("options/core.ts"):
            if intent.wants_extension or any(t in lower for t in ("extension", "options", "tab", "chrome")):
                score += 0.4
                reasons.append("extension core module boost")
            if any(is_extension_tab_path(p) for p in top10_paths):
                score += 0.35
                reasons.append("tab↔core graph pattern")

        # Demote extension/scripts/webpack noise for engine-focused tasks
        if wants_engine and not intent.wants_extension:
            if is_extension_noise_path(cand.path) or is_extension_tab_path(cand.path):
                score -= 0.55
                reasons.append("extension demotion for engine task")
            if norm.startswith("scripts/") and not intent.wants_scripts:
                score -= 0.45
                reasons.append("scripts demotion")
            if "webpack" in norm:
                score -= 0.5
                reasons.append("webpack demotion")

        if is_script_noise_path(cand.path) and not intent.wants_scripts:
            score -= 0.35
            reasons.append("script noise demotion")

        # Test files for bugfix tasks
        if task_type == "bugfix" and is_test_path(cand.path) and norm.startswith("tests/"):
            score += 0.25
            reasons.append("bugfix test surface boost")
            if any(_normalize(p).startswith("src/engine/") for p in top10_paths):
                score += 0.42
                reasons.append("engine test bundle co-rank")

        if task_type == "database" and is_test_path(cand.path):
            score += 0.3
            reasons.append("database task test boost")

        # v6 structural surface-mismatch demotion (no-op when expected_surface == "")
        if expected_surface:
            surf = classify_surface(cand.path)
            pen = surface_mismatch_penalty(cand.path, expected_surface, _surface_wanted(surf, intent))
            if pen:
                score += pen
                reasons.append(f"surface mismatch ({surf} vs {expected_surface})")

        cand.v5_score = score
        cand.explanation = "; ".join(reasons) if reasons else "baseline fusion score"
        scored.append((score, cand, reasons))

    scored_by_path = {c.path: (s, c, r) for s, c, r in scored}
    final_paths = [c.path for c in pool[:top_k]]

    def _slot_score(path: str) -> float:
        s, _, _ = scored_by_path.get(path, (0.0, None, []))
        norm = _normalize(path)
        if wants_engine and not intent.wants_extension:
            if is_extension_path(path) or is_extension_noise_path(path) or is_extension_tab_path(path):
                s -= 0.65
            if norm.startswith("scripts/") and not intent.wants_scripts:
                s -= 0.55
            if "webpack" in norm:
                s -= 0.6
        return s

    def _is_evictable(path: str) -> bool:
        norm = _normalize(path)
        if wants_engine and not intent.wants_extension:
            if is_extension_path(path) or norm.startswith("scripts/") or "webpack" in norm:
                return True
        if is_script_noise_path(path) and not intent.wants_scripts:
            return True
        return False

    # Promote strong tail candidates (11-30) by evicting noise slots only
    tail_promotions = sorted(
        [x for x in scored[top_k:] if _is_strong_v5_promotion(x[1].explanation)],
        key=lambda x: -x[0],
    )
    for score, cand, _reasons in tail_promotions:
        if cand.path in final_paths:
            continue
        evictable = [p for p in final_paths if _is_evictable(p)]
        if not evictable:
            continue
        victim = min(evictable, key=_slot_score)
        if score > _slot_score(victim) + 0.02:
            final_paths[final_paths.index(victim)] = cand.path

    # Demote extension/script noise in engine tasks
    if wants_engine:
        for path in list(final_paths):
            if not _is_evictable(path):
                continue
            replacement = None
            for score, cand, _ in sorted(scored[top_k:], key=lambda x: -x[0]):
                if cand.path in final_paths:
                    continue
                if score > _slot_score(path):
                    replacement = cand.path
                    break
            if replacement:
                final_paths[final_paths.index(path)] = replacement

    final_paths.sort(key=lambda p: (-_slot_score(p), path_to_idx.get(p, 99)))
    top = [scored_by_path[p] for p in final_paths if p in scored_by_path][:top_k]

    out_candidates: List[RerankCandidate] = []
    position_changes: List[Dict[str, Any]] = []
    for rank_after, (score, cand, reasons) in enumerate(top, 1):
        cand.rank_before = path_to_idx.get(cand.path, 99)
        cand.rank_after = rank_after
        cand.rank_delta = cand.rank_before - rank_after
        cand.v5_score = score
        out_candidates.append(cand)
        if cand.rank_delta != 0:
            position_changes.append({
                "path": cand.path,
                "rank_before": cand.rank_before,
                "rank_after": rank_after,
                "delta": cand.rank_delta,
                "explanation": cand.explanation,
            })

    return RerankResult(
        top_paths=[c.path for c in out_candidates],
        candidates=out_candidates,
        cache_hit=False,
        fallback=True,
        position_changes=position_changes,
    )


_SURFACE_NEIGHBOURS_RR = {
    "server": {"database", "engine", "config"},
    "engine": {"server", "tests"},
    "database": {"server", "tests"},
    "extension": {"server"},
    "scripts": {"server", "engine", "config"},
    "config": {"server", "docs"},
    "docs": {"server", "config"},
}


def _task_keywords(task_description: str) -> set:
    return {w for w in re.findall(r"[a-zA-Z_][\w]{3,}", task_description.lower())}


def _minmax(values: List[float]) -> Tuple[float, float, float]:
    if not values:
        return 0.0, 1.0, 1.0
    lo, hi = min(values), max(values)
    return lo, hi, (hi - lo) or 1.0


def _surface_align(path: str, expected_surface: str) -> float:
    if not expected_surface:
        return 0.0
    surf = classify_surface(path)
    if surf == expected_surface:
        return 1.0
    if surf in _SURFACE_NEIGHBOURS_RR.get(expected_surface, set()):
        return 0.4
    return 0.0


def semantic_rerank(
    pool: List[RerankCandidate],
    task_description: str,
    task_type: str,
    top_k: int = 10,
    expected_surface: str = "",
) -> RerankResult:
    """v6 P2 stage-2: rescore the pruned top-30 on a *normalized* composite, re-sort to top-10.

    The conservative v5 selection anchors on pool[:10] and only evicts noise, so a
    strong rank-25 file never converts. Here every signal (deterministic v5 score,
    vector, lexical, symbol/graph evidence, surface alignment, keyword overlap) is
    min-max normalized across the window and combined with fixed global weights — so
    multi-signal evidence can overtake a file that is merely high on raw fusion mass.
    All weights are global; never task-id or path specific.
    """
    # Run the proven deterministic scorer first — it sets cand.v5_score (test co-rank,
    # pair-boost, surface demotion already folded in) and gives the conservative baseline.
    det = deterministic_rerank(pool, task_description, task_type, top_k=top_k, expected_surface=expected_surface)
    keywords = _task_keywords(task_description)
    baseline_order = {p: i for i, p in enumerate(det.top_paths)}

    v5_lo, _v5_hi, v5_span = _minmax([c.v5_score for c in pool])
    vec_lo, _vec_hi, vec_span = _minmax([c.vector_score for c in pool])
    lex_lo, _lex_hi, lex_span = _minmax([c.lexical_score for c in pool])
    card_lo, _card_hi, card_span = _minmax([c.card_score for c in pool])
    has_card = any(c.card_score > 0 for c in pool)

    composites: List[Tuple[float, RerankCandidate]] = []
    for cand in pool:
        nf = (cand.v5_score - v5_lo) / v5_span
        nv = (cand.vector_score - vec_lo) / vec_span
        nl = (cand.lexical_score - lex_lo) / lex_span
        summ = cand.summary.lower() if cand.summary else ""
        kw_overlap = min(sum(1 for kw in keywords if kw in summ) / 5.0, 1.0) if keywords else 0.0
        composite = (
            0.42 * nf
            + 0.22 * nv
            + 0.10 * nl
            + 0.12 * (1.0 if cand.symbol_score > 0 else 0.0)
            + 0.06 * (1.0 if cand.graph_score > 0 else 0.0)
            + 0.18 * _surface_align(cand.path, expected_surface)
            + 0.10 * kw_overlap
        )
        # v6 P3: file-card role signal (moderate weight, never card-first; no-op when
        # cards disabled because card_score stays 0). Surface-gated: full weight on the
        # routed surface, damped off-route so cards don't pull unrelated files.
        if has_card:
            ncard = (cand.card_score - card_lo) / card_span
            gate = 1.0 if (not expected_surface or _surface_align(cand.path, expected_surface) > 0) else 0.4
            card_bonus = 0.16 * ncard * gate
            if card_bonus > 0:
                composite += card_bonus
                if cand.card_score > 0:
                    cand.explanation = (cand.explanation + "; " if cand.explanation else "") + "card role signal"
        composites.append((composite, cand))
    composites.sort(key=lambda x: x[0], reverse=True)

    pool_idx = {c.path: i + 1 for i, c in enumerate(pool)}
    top = composites[:top_k]
    max_c = top[0][0] if top else 1.0
    min_c = top[-1][0] if top else 0.0
    span = (max_c - min_c) or 1.0

    out: List[RerankCandidate] = []
    position_changes: List[Dict[str, Any]] = []
    for rank_after, (composite, cand) in enumerate(top, 1):
        cand.rank_before = pool_idx.get(cand.path, 99)
        cand.rank_after = rank_after
        cand.rank_delta = cand.rank_before - rank_after
        cand.confidence = round(min(1.0, 0.5 + 0.5 * (composite - min_c) / span), 3)
        # promotion note: was this file outside the conservative deterministic top-10?
        if cand.path not in baseline_order:
            cand.explanation = (cand.explanation + "; " if cand.explanation else "") + "promoted by semantic rescore"
        out.append(cand)
        if cand.rank_delta != 0:
            position_changes.append({
                "path": cand.path,
                "rank_before": cand.rank_before,
                "rank_after": rank_after,
                "delta": cand.rank_delta,
                "explanation": cand.explanation,
            })

    # 3. Excluded-but-relevant: strong candidates just past the cutoff.
    cutoff = min_c
    excluded_relevant: List[Dict[str, Any]] = []
    for composite, cand in composites[top_k:top_k + 6]:
        if composite >= cutoff * 0.92:
            excluded_relevant.append({
                "path": cand.path,
                "composite": round(composite, 4),
                "surface": classify_surface(cand.path),
                "reason": cand.explanation or "near-cutoff candidate",
            })

    return RerankResult(
        top_paths=[c.path for c in out],
        candidates=out,
        cache_hit=False,
        fallback=True,
        position_changes=position_changes,
        stage="semantic",
        excluded_relevant=excluded_relevant,
    )


async def _llm_rerank(
    pool: List[RerankCandidate],
    task_description: str,
    task_type: str,
    top_k: int,
) -> Optional[RerankResult]:
    """Optional LLM rerank; returns None on failure so caller can fall back."""
    from brain.llm.router import TaskKind, get_model_router

    lines = []
    for i, c in enumerate(pool[:30], 1):
        lines.append(
            f"{i}. {c.path} | fusion={c.reranker_score:.3f} vec={c.vector_score:.3f} "
            f"lex={c.lexical_score:.3f} | {c.summary[:120] if c.summary else ''}"
        )
    prompt = (
        f"Task ({task_type}): {task_description}\n\n"
        f"Rank these {len(pool)} files by relevance. Return JSON:\n"
        f'{{"ranked_paths": ["path1", ...], "explanations": {{"path1": "reason", ...}}}}\n\n'
        + "\n".join(lines)
    )
    try:
        router = get_model_router()
        raw = await router.llm(TaskKind.CLASSIFICATION).generate(
            prompt=prompt,
            system_instruction="Return valid JSON only with ranked_paths (max 10) and explanations map.",
        )
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\n", "", cleaned)
            cleaned = re.sub(r"\n```$", "", cleaned)
        data = json.loads(cleaned)
        ranked_paths = [p for p in data.get("ranked_paths", []) if any(c.path == p for c in pool)]
        explanations = data.get("explanations", {})
        if not ranked_paths:
            return None
        path_to_cand = {c.path: c for c in pool}
        out: List[RerankCandidate] = []
        position_changes: List[Dict[str, Any]] = []
        path_to_idx = {c.path: i + 1 for i, c in enumerate(pool)}
        for rank_after, path in enumerate(ranked_paths[:top_k], 1):
            cand = path_to_cand.get(path)
            if not cand:
                continue
            cand.rank_before = path_to_idx.get(path, 99)
            cand.rank_after = rank_after
            cand.rank_delta = cand.rank_before - rank_after
            cand.explanation = explanations.get(path, "LLM rerank")
            cand.v5_score = cand.reranker_score + (top_k - rank_after) * 0.01
            out.append(cand)
            if cand.rank_delta != 0:
                position_changes.append({
                    "path": path,
                    "rank_before": cand.rank_before,
                    "rank_after": rank_after,
                    "delta": cand.rank_delta,
                    "explanation": cand.explanation,
                })
        # Fill remaining slots from deterministic order if LLM returned fewer than top_k
        seen = {c.path for c in out}
        if len(out) < top_k:
            det = deterministic_rerank(pool, task_description, task_type, top_k=top_k)
            for path in det.top_paths:
                if path not in seen and len(out) < top_k:
                    out.append(path_to_cand[path])
                    seen.add(path)
        return RerankResult(
            top_paths=[c.path for c in out[:top_k]],
            candidates=out[:top_k],
            used_llm=True,
            fallback=False,
            position_changes=position_changes,
            estimated_cost_usd=0.002,
            stage="llm",
        )
    except Exception as exc:
        logger.warning(f"LLM rerank failed, using deterministic fallback: {exc}")
        return None


async def rerank_top_k(
    pool: List[RerankCandidate],
    task_description: str,
    task_type: str,
    repo_hash: str = "",
    top_k: int = 10,
    use_cache: bool = True,
    use_llm: Optional[bool] = None,
    expected_surface: str = "",
    two_stage: bool = False,
    commit_hash: str = "",
    prune_limit: Optional[int] = None,
) -> RerankResult:
    """Rerank the recall pool to top-k.

    v5 path (``two_stage=False``): single deterministic pass (frozen behaviour).
    v6 P2 path (``two_stage=True``): prune top-100 → top-30, then an optional
    LLM rerank (with timeout) falling back deterministically to the semantic
    rescore stage. Cache key binds reranker version + commit + candidate-set + surface,
    so v5 and v6 caches never collide and a logic bump invalidates stale entries.
    """
    t0 = time.perf_counter()
    candidate_paths = [c.path for c in pool]
    llm_enabled = use_llm if use_llm is not None else getattr(settings, "RETRIEVAL_USE_LLM_RERANK", False)
    llm_live = bool(llm_enabled) and settings.DEFAULT_LLM_PROVIDER.lower() != "mock"
    version_tag = RERANKER_VERSION + ("_2s" if two_stage else "_1s") + ("_llm" if llm_live else "")
    cache_key = build_cache_key(repo_hash or "local", task_description, candidate_paths)
    cache_key = f"{cache_key}_{version_tag}"
    if expected_surface:
        cache_key = f"{cache_key}_{expected_surface}"
    if commit_hash:
        # Snapshot revisions share the literal prefix "snapshot:". Bind the
        # entire source identity, including its digest, in every rerank mode.
        revision_key = hashlib.sha256(commit_hash.encode("utf-8")).hexdigest()[:16]
        cache_key = f"{revision_key}_{cache_key}"

    if use_cache:
        cached = _load_cache(cache_key)
        if cached:
            cached.latency_ms = (time.perf_counter() - t0) * 1000
            cached.cache_key = cache_key
            cached.cache_hit = True
            return cached

    # Stage 1 — prune to the intermediate window (incoming order is reranker_score).
    if two_stage:
        prune = prune_limit or getattr(settings, "RETRIEVAL_RERANK_PRUNE_LIMIT", 30)
        stage1 = pool[:prune]
    else:
        stage1 = pool

    # Stage 2 — optional LLM (timeout-guarded) → deterministic/semantic fallback.
    result: Optional[RerankResult] = None
    fallback_reason = ""
    if llm_enabled:
        if not llm_live:
            fallback_reason = "llm_provider_mock"
        else:
            timeout_s = getattr(settings, "RETRIEVAL_RERANK_TIMEOUT_S", 8.0)
            try:
                result = await asyncio.wait_for(
                    _llm_rerank(stage1, task_description, task_type, top_k), timeout=timeout_s
                )
                if result is None:
                    fallback_reason = "llm_returned_empty"
            except asyncio.TimeoutError:
                logger.warning(f"LLM rerank timed out after {timeout_s}s; deterministic fallback")
                fallback_reason = "llm_timeout"
                result = None
    elif two_stage:
        fallback_reason = "llm_disabled"

    if result is None:
        if two_stage:
            result = semantic_rerank(
                stage1, task_description, task_type, top_k=top_k, expected_surface=expected_surface
            )
        else:
            result = deterministic_rerank(
                stage1, task_description, task_type, top_k=top_k, expected_surface=expected_surface
            )
        result.fallback_reason = fallback_reason

    result.cache_key = cache_key
    result.reranker_version = version_tag
    result.latency_ms = (time.perf_counter() - t0) * 1000
    if use_cache:
        _save_cache(cache_key, result)
    return result
