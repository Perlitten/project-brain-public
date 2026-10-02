"""Audit middleware: records every mutating API call with its principal.

Scope (B7c): POST/PUT/DELETE/PATCH on every route, whatever the outcome —
a denied attempt is recorded as ``denied`` with the principal it came from
(or ``unresolved`` when authentication itself failed). Reads are not
audited (volume).
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from brain.audit.events import record_audit_event

AUDITED_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})


class AuditLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        if request.method not in AUDITED_METHODS:
            return response
        principal = getattr(request.state, "principal", None)
        await record_audit_event(
            principal_name=principal.name if principal else "unresolved",
            principal_id=principal.id if principal and principal.id > 0 else None,
            credential_id=principal.credential_id if principal else None,
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            request_id=getattr(request.state, "request_id", None),
        )
        return response
