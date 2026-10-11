"""Whether recorded rules actually do anything.

A rule costs nothing to write down and is worth nothing unless it reaches the
work. Two observable moments are recorded here, and they answer different
questions:

- ``delivered`` — the rule was put into a context pack an agent asked for, so
  the agent saw it. Answers "does memory reach the agent at all?".
- ``flagged`` — a diff review cited the rule against a real change. Answers
  "did this rule catch something?".

Both writes are best-effort: accounting must never be the reason a context
pack or a review fails.
"""

from __future__ import annotations

from typing import Iterable, Optional

from loguru import logger

from brain.database.models import RuleUsageEvent
from brain.database.session import async_session_factory

DELIVERED = "delivered"
FLAGGED = "flagged"


async def record_rule_usage(
    rule_ids: Iterable[str],
    kind: str,
    *,
    repository_id: Optional[int] = None,
    context_pack_id: Optional[int] = None,
    principal_name: Optional[str] = None,
) -> int:
    """Append one event per rule. Returns how many rows were written."""
    unique: list[str] = []
    seen: set[str] = set()
    for rule_id in rule_ids:
        key = str(rule_id).strip()[:255]
        if key and key not in seen:
            seen.add(key)
            unique.append(key)
    if not unique:
        return 0
    principal = principal_name[:128] if principal_name else None
    try:
        async with async_session_factory() as session:
            session.add_all(
                [
                    RuleUsageEvent(
                        rule_id=rule_id,
                        kind=kind,
                        repository_id=repository_id,
                        context_pack_id=context_pack_id,
                        principal_name=principal,
                    )
                    for rule_id in unique
                ]
            )
            await session.commit()
        return len(unique)
    except Exception as exc:
        logger.warning("Rule usage telemetry write failed ({}): {}", kind, type(exc).__name__)
        return 0


async def record_rules_delivered(
    rule_ids: Iterable[str],
    *,
    repository_id: Optional[int] = None,
    context_pack_id: Optional[int] = None,
    principal_name: Optional[str] = None,
) -> int:
    return await record_rule_usage(
        rule_ids,
        DELIVERED,
        repository_id=repository_id,
        context_pack_id=context_pack_id,
        principal_name=principal_name,
    )


async def record_rules_flagged(
    rule_ids: Iterable[str],
    *,
    repository_id: Optional[int] = None,
    principal_name: Optional[str] = None,
) -> int:
    return await record_rule_usage(
        rule_ids,
        FLAGGED,
        repository_id=repository_id,
        principal_name=principal_name,
    )
