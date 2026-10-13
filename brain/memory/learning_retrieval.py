"""Bounded, live L3 projection for task contexts (not a source of code truth)."""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from loguru import logger
from sqlalchemy import func, or_, select

from brain.config.settings import settings
from brain.database.models import Learning
from brain.database.session import async_session_factory
from brain.memory.learning_store import _scope_clause

_TOKEN = re.compile(r"[^\W\d][\w-]{2,}", re.UNICODE)
_STOP = frozenset("the and for with from this that how what where does can you fix add implement explain current project brain use task code".split())

# Semantic pass: how many nearest stored vectors to pull before merging.
SEMANTIC_CANDIDATES = 32
# Score weights of the hybrid ranking (cosine, lexical overlap share, confidence).
_W_SEMANTIC, _W_LEXICAL, _W_CONFIDENCE = 0.6, 0.3, 0.1
# A learning found only by the semantic pass must be at least this close.
SEMANTIC_MIN_SIMILARITY = 0.45


def learning_query_terms(task: str) -> list[str]:
    """Keep Unicode and code identifiers, escape SQL wildcards at the caller."""
    return list(dict.fromkeys(t.casefold() for t in _TOKEN.findall(task)
                             if t.casefold() not in _STOP))[:16]


def _live_scoped(repo_scope: str | None):
    return (
        Learning.status == "active",
        or_(Learning.valid_until.is_(None), Learning.valid_until > func.now()),
        _scope_clause(repo_scope),
    )


async def _embed_query(task: str) -> tuple[list[float] | None, str | None]:
    """One bounded embedding call for the query; (None, None) when unavailable.

    Never backfills stored vectors: a learning without an embedding of the
    current model simply does not take part in the semantic pass.
    """
    if not settings.MEMORY_RETRIEVAL_SEMANTIC:
        return None, None
    try:
        from brain.llm import get_embedding_provider

        embedder = get_embedding_provider()
        model = getattr(embedder, "model", None) or getattr(embedder, "provider", "unknown")
        vec = await asyncio.wait_for(embedder.embed(task), timeout=settings.MEMORY_RETRIEVAL_EMBED_TIMEOUT_S)
        return (list(vec) if vec else None), model
    except Exception as exc:  # timeout, provider down, missing config
        logger.debug(f"learning retrieval: semantic pass skipped ({type(exc).__name__}: {exc})")
        return None, None


async def _semantic_rows(
    session, query_vec: list[float], model: str, repo_scope: str | None,
) -> tuple[dict[int, float], dict[int, Learning]]:
    """(id → cosine similarity, id → row) for the nearest stored learnings of the current model."""
    if settings.BRAIN_STORAGE_MODE == "lite":
        from brain.search.similarity import cosine_similarity
        lite_rows = list((await session.scalars(select(Learning).where(
            *_live_scoped(repo_scope), Learning.embedding.isnot(None), Learning.embedding_model == model
        ).order_by(Learning.id).limit(50_001))).all())
        if len(lite_rows) > 50_000:
            raise ValueError("Lite memory vector limit exceeded")
        ranked = sorted(((max(0.0, cosine_similarity(query_vec, row.embedding)), row) for row in lite_rows
                         if len(row.embedding) == len(query_vec)), key=lambda item: (-item[0], item[1].id))[:SEMANTIC_CANDIDATES]
        return {row.id: score for score, row in ranked}, {row.id: row for _, row in ranked}
    distance = Learning.embedding.cosine_distance(query_vec)
    stmt = (
        select(Learning, distance.label("distance"))
        .where(*_live_scoped(repo_scope), Learning.embedding.isnot(None), Learning.embedding_model == model)
        .order_by(distance)
        .limit(SEMANTIC_CANDIDATES)
    )
    sims: dict[int, float] = {}
    rows: dict[int, Learning] = {}
    for learning, dist in (await session.execute(stmt)).all():
        if dist is None:
            continue
        sims[learning.id] = max(0.0, 1.0 - float(dist))
        rows[learning.id] = learning
    return sims, rows


async def select_learnings_for_context(
    task: str, repo_scope: str | None, *, max_count: int = 3, max_bytes: int = 1200,
) -> list[dict[str, Any]]:
    """Read applicable active facts each time, with bounded rows and no LLM call.

    Hybrid: a pgvector pass over stored embeddings (one bounded query-embedding
    call, no backfill) plus the lexical pass over statements. Ranking is
    0.6*cosine + 0.3*lexical overlap + 0.1*confidence; a learning seen only by
    the semantic pass must clear ``SEMANTIC_MIN_SIMILARITY``. Without an
    embedder the function degrades to the lexical pass alone.
    """
    terms = learning_query_terms(task)
    if not terms or max_count <= 0 or max_bytes < 2:
        return []
    query_vec, model = await _embed_query(task)
    matches = [Learning.statement.ilike("%" + term.replace("\\", "\\\\")
               .replace("%", "\\%").replace("_", "\\_") + "%", escape="\\") for term in terms]
    sims: dict[int, float] = {}
    semantic_rows: dict[int, Learning] = {}
    async with async_session_factory() as session:
        if query_vec and model:
            try:
                sims, semantic_rows = await _semantic_rows(session, query_vec, model, repo_scope)
            except Exception as exc:
                logger.debug(f"learning retrieval: semantic query failed ({type(exc).__name__}: {exc})")
                sims, semantic_rows = {}, {}
        rows = list((await session.execute(
            select(Learning).where(
                *_live_scoped(repo_scope), or_(*matches),
            ).order_by(Learning.confidence.desc(), Learning.updated_at.desc(), Learning.id.desc()).limit(64)
        )).scalars().all())
    by_id: dict[int, Learning] = {row.id: row for row in rows}
    for lid, row in semantic_rows.items():
        by_id.setdefault(lid, row)
    term_set = set(terms)
    scored = []
    for row in by_id.values():
        words = set(learning_query_terms(row.statement))
        overlap = len(term_set & words)
        sim = sims.get(row.id, 0.0)
        if not overlap and sim < SEMANTIC_MIN_SIMILARITY:
            continue
        lexical = overlap / max(1, len(term_set))
        score = _W_SEMANTIC * sim + _W_LEXICAL * lexical + _W_CONFIDENCE * float(row.confidence or 0.0)
        scored.append((score, overlap, row.confidence, row.id, row))
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _, _, _, _, row in sorted(scored, key=lambda item: item[:4], reverse=True):
        statement = " ".join(row.statement.split())
        if statement.casefold() in seen:
            continue
        item = {"id": row.id, "statement": statement[:400], "confidence": row.confidence,
                "repo_scope": row.repo_scope, "promoted_from": row.promoted_from,
                "kind": "learned_guidance"}
        if len(json.dumps([*selected, item], ensure_ascii=False, separators=(",", ":")).encode()) > max_bytes:
            continue
        selected.append(item)
        seen.add(statement.casefold())
        if len(selected) >= max_count:
            break
    return selected
