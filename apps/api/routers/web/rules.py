"""Decision and rule listings for the memory screens."""
from __future__ import annotations

from collections import defaultdict
from typing import Any
from fastapi import Depends, HTTPException, Query
from loguru import logger
from sqlalchemy import String, cast, func, literal, select
from apps.api import team_access
from apps.api.auth import require_principal
from brain.auth.principals import ResolvedPrincipal
from brain.database.models import Decision, Repository, Rule
from brain.database.session import async_session_factory
from brain.memory.repo_scope import repository_scope_clause
from ._common import router


def _decision_status(value: Any) -> str:
    raw = str(value or "").lower()
    if raw in {"active", "accepted"}:
        return "accepted"
    if raw in {"proposed", "draft", "pending"}:
        return "proposed"
    return (
        "superseded"
        if raw in {"deprecated", "superseded", "historical", "replaced", "inactive", "rejected"}
        else (raw or "unknown")
    )


def _rule_severity(value: Any) -> str:
    raw = str(value or "").lower()
    if raw in {"high", "error", "critical", "block", "blocker"}:
        return "block"
    if raw in {"medium", "warn", "warning"}:
        return "warn"
    return "advise"


@router.get("/decisions")
async def web_decisions(page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=500),
                        q: str | None = Query(None, max_length=500), status: str | None = Query(None, max_length=50),
                        repository_id: int | None = None,
                        principal: ResolvedPrincipal = Depends(require_principal)) -> dict[str, Any]:
    async with async_session_factory() as session:
        base = select(Decision)
        if repository_id is not None:
            repository = await session.get(Repository, repository_id)
            if repository is None:
                raise HTTPException(status_code=404, detail="Repository not found")
            base = base.where(repository_scope_clause(Decision, repository.path))
        if q:
            needle = f"%{q.strip()}%"
            text = cast(Decision.id, String) + literal(" ") + func.coalesce(Decision.title, "") + literal(" ") + func.coalesce(Decision.description, "")
            base = base.where(text.ilike(needle))
        rows = list((await session.execute(base.order_by(Decision.id))).scalars().all())
    readable = await team_access.readable_memory_rows(principal, rows)
    facets: dict[str, int] = defaultdict(int)
    for row in readable:
        facets[_decision_status(row.status)] += 1
    filtered = [row for row in readable if not status or _decision_status(row.status) == _decision_status(status)]
    total = len(filtered)
    rows = filtered[(page - 1) * page_size:page * page_size]
    return {"total": total, "page": page, "page_size": page_size, "total_pages": max(1, (total + page_size - 1) // page_size),
            "facets": {"status": dict(facets)}, "scope": "repository" if repository_id is not None else "global",
            "decisions": [{"id": r.id, "title": r.title, "description": r.description, "status": _decision_status(r.status), "raw_status": r.status,
                           "repo_path": r.repo_path, "version": r.version or 1, "current": r.superseded_by_id is None, "date": r.date.isoformat() if r.date else None, "reason": r.reason,
                           "consequences": r.consequences, "affected_features": r.affected_features, "affected_modules": r.affected_modules,
                           "affected_files": r.affected_files} for r in rows]}


@router.get("/rules")
async def web_rules(page: int = Query(1, ge=1, le=1000000), page_size: int = Query(50, ge=1, le=500),
                    q: str | None = Query(None, max_length=500), status: str | None = Query(None, max_length=50),
                    severity: str | None = Query(None, max_length=50), repository_id: int | None = None,
                    principal: ResolvedPrincipal = Depends(require_principal)) -> dict[str, Any]:
    async with async_session_factory() as session:
        base = select(Rule).where(func.coalesce(func.lower(Rule.status), "").not_in(("inactive", "disabled", "deprecated")))
        if repository_id is not None:
            repository = await session.get(Repository, repository_id)
            if repository is None:
                raise HTTPException(status_code=404, detail="Repository not found")
            base = base.where(repository_scope_clause(Rule, repository.path))
        if q:
            needle = f"%{q.strip()}%"
            text = cast(Rule.id, String) + literal(" ") + func.coalesce(Rule.name, "") + literal(" ") + func.coalesce(Rule.description, "")
            base = base.where(text.ilike(needle))
        if status:
            base = base.where(func.lower(Rule.status) == status.lower())
        rows = list((await session.execute(base.order_by(Rule.id))).scalars().all())
    readable = await team_access.readable_memory_rows(principal, rows)
    facets: dict[str, int] = defaultdict(int)
    for row in readable:
        facets[_rule_severity(row.severity)] += 1
    filtered = [row for row in readable if not severity or _rule_severity(row.severity) == _rule_severity(severity)]
    total = len(filtered)
    rows = filtered[(page - 1) * page_size:page * page_size]
    # How each rule on this page has been used. Counted only for the page, so a
    # large rule set costs the same as a small one. Usage keys are omitted
    # entirely until something has been recorded: a rendered 0 would claim the
    # rule never fired, when the truth is that nothing was counting.
    usage = await _rule_usage_for_page([r.id for r in rows], repository_id)
    counts = usage["counts"] if usage["measurement_started_at"] else {}
    measured = bool(usage["measurement_started_at"])

    def shape(rule: Any) -> dict[str, Any]:
        shaped = {"id": rule.id, "name": rule.name, "description": rule.description, "status": rule.status,
                  "severity": _rule_severity(rule.severity), "raw_severity": rule.severity, "type": rule.type,
                  "applies_to": rule.applies_to, "repo_path": rule.repo_path,
                  # Edits compare-and-swap on this version; history copies are never edited.
                  "version": rule.version or 1, "current": rule.superseded_by_id is None}
        if measured:
            seen = counts.get(rule.id) or {}
            shaped["delivered_30d"] = int(seen.get("delivered") or 0)
            shaped["flagged_30d"] = int(seen.get("flagged") or 0)
            # Legacy name the web client already reads: "caught something".
            shaped["hits_30d"] = shaped["flagged_30d"]
        return shaped

    return {"total": total, "page": page, "page_size": page_size, "total_pages": max(1, (total + page_size - 1) // page_size),
            "facets": {"severity": dict(facets)}, "scope": "repository" if repository_id is not None else "global",
            "usage": {"measurement_started_at": usage["measurement_started_at"], "window_days": 30},
            "rules": [shape(r) for r in rows]}


async def _rule_usage_for_page(rule_ids: list[str], repository_id: int | None) -> dict[str, Any]:
    """Usage counts, or an honest blank when the store cannot answer."""
    from brain.insights.agent_value import rule_usage_counts

    try:
        return await rule_usage_counts(rule_ids, period_days=30, repository_id=repository_id)
    except Exception as exc:
        logger.warning("Rule usage counts unavailable: {}", type(exc).__name__)
        return {"measurement_started_at": None, "counts": {}}
