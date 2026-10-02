"""Request-correlation middleware (B8a).

Every request gets a ``request_id`` — the caller's ``X-Request-ID`` when it
is well-formed, else a fresh UUID — echoed on the response, stored on
``request.state.request_id`` for downstream consumers (audit events), and
bound into loguru's context so application logs inside the request share
the same id.
"""

from __future__ import annotations

import re
import uuid

from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


def resolve_request_id(presented: str | None) -> str:
    if presented and _REQUEST_ID_RE.match(presented):
        return presented
    return uuid.uuid4().hex


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = resolve_request_id(request.headers.get(REQUEST_ID_HEADER))
        request.state.request_id = request_id
        with logger.contextualize(request_id=request_id):
            response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
