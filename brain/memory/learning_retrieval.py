"""Bounded, live L3 projection for task contexts (not a source of code truth)."""
from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import func, or_, select

from brain.database.models import Learning
from brain.database.session import async_session_factory
from brain.memory.learning_store import _scope_clause

_TOKEN = re.compile(r"[^\W\d][\w-]{2,}", re.UNICODE)
_STOP = frozenset("the and for with from this that how what where does can you fix add implement explain current project brain use task code что как для это где почему нужно надо сделай исправь проект код".split())


def learning_query_terms(task: str) -> list[str]:
    """Keep Unicode and code identifiers, escape SQL wildcards at the caller."""
    return list(dict.fromkeys(t.casefold() for t in _TOKEN.findall(task)
                             if t.casefold() not in _STOP))[:16]


async def select_learnings_for_context(
    task: str, repo_scope: str | None, *, max_count: int = 3, max_bytes: int = 1200,
) -> list[dict[str, Any]]:
    """Read applicable active facts each time, with bounded rows and no LLM call.

    Deliberately lexical: persisted embeddings are optional and a missing vector
    must never cause unbounded embedding backfills on a context request. Semantic
    discovery remains available through the full memory/legacy context surface.
    """
    terms = learning_query_terms(task)
    if not terms or max_count <= 0 or max_bytes < 2:
        return []
    matches = [Learning.statement.ilike("%" + term.replace("\\", "\\\\")
               .replace("%", "\\%").replace("_", "\\_") + "%", escape="\\") for term in terms]
    async with async_session_factory() as session:
        rows = list((await session.execute(
            select(Learning).where(
                Learning.status == "active",
                or_(Learning.valid_until.is_(None), Learning.valid_until > func.now()),
                _scope_clause(repo_scope), or_(*matches),
            ).order_by(Learning.confidence.desc(), Learning.updated_at.desc(), Learning.id.desc()).limit(64)
        )).scalars().all())
    scored = []
    for row in rows:
        words = set(learning_query_terms(row.statement))
        overlap = len(set(terms) & words)
        if overlap:
            scored.append((overlap, row.confidence, row.id, row))
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _, _, _, row in sorted(scored, key=lambda item: item[:3], reverse=True):
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
