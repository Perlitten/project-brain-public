"""Small, task-relevant projection of normative rules and decisions."""
from __future__ import annotations

import re
from typing import Any, Iterable

from sqlalchemy import select

from brain.database.models import Decision, Rule
from brain.database.session import async_session_factory
from brain.memory.repo_scope import normalize_repo_scope, repository_scope_clause


_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_/-]{2,}")
_HISTORICAL_MARKERS = ("release", "deploy", "reindex", "checkpoint", "audit", "completed")
_SEVERITY = {"critical": 4, "high": 3, "medium": 2, "low": 1}


def _terms(value: str) -> set[str]:
    return {token.casefold() for token in _TOKEN_RE.findall(value)}


def _flatten(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(f"{key} {_flatten(item)}" for key, item in value.items())
    if isinstance(value, (list, tuple, set)):
        return " ".join(_flatten(item) for item in value)
    return str(value or "")


def _excerpt(value: str | None, limit: int = 220) -> str:
    return " ".join((value or "").split())[:limit]


def _score(text: str, task_terms: set[str], candidate_paths: Iterable[str], scope_match: bool) -> float:
    score = 4.0 if scope_match else 1.0
    text_terms = _terms(text)
    score += min(len(task_terms & text_terms), 5) * 2.0
    text_folded = text.casefold()
    score += sum(2.0 for path in candidate_paths if path.casefold() in text_folded)
    return score


async def select_relevant_normative_memory(
    task: str,
    candidate_paths: Iterable[str],
    repo_scope: str | None,
    *,
    rule_limit: int = 3,
    decision_limit: int = 3,
) -> dict[str, list[dict[str, Any]]]:
    """Return at most three concise, applicable rules and decisions.

    Historical records remain untouched in Postgres but are not injected into a
    normal runtime context. A full record is still available via the dedicated
    memory endpoints.
    """
    normalized_scope = normalize_repo_scope(repo_scope)
    paths = list(candidate_paths)
    task_terms = _terms(task)
    async with async_session_factory() as session:
        rules = list(
            (await session.execute(
                select(Rule).where(Rule.status == "active", repository_scope_clause(Rule, normalized_scope))
            )).scalars().all()
        )
        decisions = list(
            (await session.execute(select(Decision).where(Decision.status == "active"))).scalars().all()
        )

    relevant_rules: list[tuple[float, dict[str, Any]]] = []
    for rule in rules:
        text = " ".join((rule.name or "", rule.description or "", _flatten(rule.applies_to)))
        applies_text = _flatten(rule.applies_to).casefold()
        has_explicit_scope = bool(normalized_scope and normalize_repo_scope(rule.repo_path) == normalized_scope)
        # Repository ownership tells us where a rule may apply; it does not
        # prove that the rule matters to this task. Require a task or selected
        # path overlap before severity/scope ranking can admit it.
        applies = (
            bool(task_terms & _terms(text))
            or any(path.casefold() in applies_text for path in paths)
        )
        if not applies:
            continue
        score = _score(text, task_terms, paths, has_explicit_scope) + _SEVERITY.get((rule.severity or "").lower(), 0)
        if score <= (4.0 if has_explicit_scope else 1.0):
            continue
        relevant_rules.append((score, {
            "id": rule.id,
            "name": rule.name,
            "severity": rule.severity,
            "description": _excerpt(rule.description),
        }))

    relevant_decisions: list[tuple[float, dict[str, Any]]] = []
    for decision in decisions:
        decision_scope = normalize_repo_scope(decision.repo_path)
        if decision_scope not in {None, normalized_scope}:
            continue
        text = " ".join((
            decision.title or "", decision.description or "", decision.reason or "",
            _flatten(decision.affected_files), _flatten(decision.affected_modules),
        ))
        # Operational updates are durable evidence, not default runtime policy.
        if any(marker in text.casefold() for marker in _HISTORICAL_MARKERS):
            continue
        score = _score(text, task_terms, paths, decision_scope == normalized_scope)
        if score <= (4.0 if decision_scope == normalized_scope else 1.0):
            continue
        relevant_decisions.append((score, {
            "id": decision.id,
            "title": decision.title,
            "description": _excerpt(decision.description or decision.reason),
        }))

    def sort_key(item: tuple[float, dict[str, Any]]) -> tuple[float, str]:
        return (-item[0], str(item[1].get("id")))

    return {
        "rules": [item for _score_value, item in sorted(relevant_rules, key=sort_key)[:rule_limit]],
        "decisions": [item for _score_value, item in sorted(relevant_decisions, key=sort_key)[:decision_limit]],
    }
