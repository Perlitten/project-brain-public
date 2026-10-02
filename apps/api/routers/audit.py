"""Audit log read/export endpoints (B11 slice).

The audit middleware writes an ``audit_events`` row for every mutating
authenticated call; these routes let an operator read that log back — paged
JSON for inspection, CSV for export into a customer's SIEM. Both require the
``audit:read`` scope so access to the security log itself is attributable.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Iterator, Optional, Sequence

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import desc, select
from sqlalchemy.sql import Select

from apps.api.auth import require_api_key, require_scope
from brain.audit.events import OUTCOME_ALLOWED, OUTCOME_DENIED, OUTCOME_ERROR
from brain.database.models import AuditEvent
from brain.database.session import async_session_factory

router = APIRouter(prefix="/audit", tags=["audit"], dependencies=[Depends(require_api_key)])

_VALID_OUTCOMES = {OUTCOME_ALLOWED, OUTCOME_DENIED, OUTCOME_ERROR}
_MAX_PAGE = 1000
_MAX_EXPORT = 10000

_CSV_HEADER = [
    "id",
    "created_at",
    "principal_name",
    "principal_id",
    "credential_id",
    "method",
    "path",
    "status_code",
    "outcome",
    "request_id",
]


def _audit_query(
    *,
    outcome: Optional[str],
    principal: Optional[str],
    path_prefix: Optional[str],
    since: Optional[datetime],
    until: Optional[datetime],
    limit: int,
    offset: int,
) -> Select[AuditEvent]:
    stmt = select(AuditEvent).order_by(desc(AuditEvent.created_at), desc(AuditEvent.id))
    if outcome is not None:
        if outcome not in _VALID_OUTCOMES:
            raise HTTPException(
                status_code=400,
                detail=f"outcome must be one of {sorted(_VALID_OUTCOMES)}",
            )
        stmt = stmt.where(AuditEvent.outcome == outcome)
    if principal:
        stmt = stmt.where(AuditEvent.principal_name == principal)
    if path_prefix:
        stmt = stmt.where(AuditEvent.path.like(f"{path_prefix}%"))
    if since is not None:
        stmt = stmt.where(AuditEvent.created_at >= since)
    if until is not None:
        stmt = stmt.where(AuditEvent.created_at <= until)
    return stmt.limit(limit).offset(offset)


def _serialize(event: AuditEvent) -> dict[str, object]:
    return {
        "id": event.id,
        "created_at": event.created_at.isoformat() if event.created_at else None,
        "principal_name": event.principal_name,
        "principal_id": event.principal_id,
        "credential_id": event.credential_id,
        "method": event.method,
        "path": event.path,
        "status_code": event.status_code,
        "outcome": event.outcome,
        "request_id": event.request_id,
    }


async def _fetch(stmt: Select[AuditEvent]) -> Sequence[AuditEvent]:
    async with async_session_factory() as session:
        result = await session.scalars(stmt)
        return result.all()


@router.get("/events", dependencies=[Depends(require_scope("audit:read"))])
async def list_audit_events(
    outcome: Optional[str] = None,
    principal: Optional[str] = None,
    path_prefix: Optional[str] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    limit: int = Query(default=100, ge=1, le=_MAX_PAGE),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    """Paged JSON view over the audit log, newest first."""
    rows = await _fetch(
        _audit_query(
            outcome=outcome,
            principal=principal,
            path_prefix=path_prefix,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
        )
    )
    return {"count": len(rows), "events": [_serialize(row) for row in rows]}


@router.get("/export", dependencies=[Depends(require_scope("audit:read"))])
async def export_audit_events(
    outcome: Optional[str] = None,
    principal: Optional[str] = None,
    path_prefix: Optional[str] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    limit: int = Query(default=5000, ge=1, le=_MAX_EXPORT),
) -> StreamingResponse:
    """CSV export for SIEM ingestion, same filters as ``/audit/events``."""
    rows = await _fetch(
        _audit_query(
            outcome=outcome,
            principal=principal,
            path_prefix=path_prefix,
            since=since,
            until=until,
            limit=limit,
            offset=0,
        )
    )

    def _lines() -> Iterator[str]:
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(_CSV_HEADER)
        yield buf.getvalue()
        for event in rows:
            buf.seek(0)
            buf.truncate(0)
            writer.writerow(
                [
                    event.id,
                    event.created_at.isoformat() if event.created_at else "",
                    event.principal_name,
                    event.principal_id if event.principal_id is not None else "",
                    event.credential_id if event.credential_id is not None else "",
                    event.method,
                    event.path,
                    event.status_code,
                    event.outcome,
                    event.request_id or "",
                ]
            )
            yield buf.getvalue()

    return StreamingResponse(
        _lines(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="audit_events.csv"'},
    )
