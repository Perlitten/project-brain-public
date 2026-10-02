"""Persist an audit event for an API mutation.

The transport middleware (apps/api/audit_middleware.py) calls
``record_audit_event`` after each mutating authenticated request — including
denied ones, which are often the most valuable rows. Writes open their own
session: the event is durable even when the request itself fails.
"""

from __future__ import annotations

from typing import Optional

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from brain.database.models import AuditEvent
from brain.database.session import async_session_factory

OUTCOME_ALLOWED = "allowed"
OUTCOME_DENIED = "denied"
OUTCOME_ERROR = "error"


def outcome_for_status(status_code: int) -> str:
    if status_code in (401, 403):
        return OUTCOME_DENIED
    if status_code >= 400:
        return OUTCOME_ERROR
    return OUTCOME_ALLOWED


async def write_audit_event(
    session: AsyncSession,
    *,
    principal_name: str,
    principal_id: Optional[int],
    credential_id: Optional[int],
    method: str,
    path: str,
    status_code: int,
    request_id: Optional[str] = None,
) -> AuditEvent:
    event = AuditEvent(
        principal_name=principal_name[:128],
        principal_id=principal_id,
        credential_id=credential_id,
        method=method[:8],
        path=path[:512],
        status_code=status_code,
        outcome=outcome_for_status(status_code),
        request_id=request_id[:64] if request_id else None,
    )
    session.add(event)
    return event


async def record_audit_event(
    *,
    principal_name: str,
    principal_id: Optional[int],
    credential_id: Optional[int],
    method: str,
    path: str,
    status_code: int,
    request_id: Optional[str] = None,
) -> None:
    """Best-effort durable write; audit failures must never break traffic."""
    try:
        async with async_session_factory() as session:
            async with session.begin():
                await write_audit_event(
                    session,
                    principal_name=principal_name,
                    principal_id=principal_id,
                    credential_id=credential_id,
                    method=method,
                    path=path,
                    status_code=status_code,
                    request_id=request_id,
                )
    except Exception as exc:  # broad on purpose — audit must not 500 the request
        logger.warning("audit event write failed for {} {}: {}", method, path, exc)
