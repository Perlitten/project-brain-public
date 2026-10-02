from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from brain.config.settings import settings


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject bodies larger than API_MAX_REQUEST_BODY_BYTES (0 disables).

    Mirrors the nginx `client_max_body_size` cap for deployments that expose
    uvicorn directly. Enforced on the declared Content-Length, matching how
    proxies short-circuit oversized requests before streaming the body.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        limit = int(settings.API_MAX_REQUEST_BODY_BYTES)
        if limit > 0:
            raw_length = request.headers.get("content-length")
            if raw_length:
                try:
                    declared = int(raw_length)
                except ValueError:
                    declared = -1
                if declared < 0 or declared > limit:
                    return JSONResponse(
                        status_code=413,
                        content={"detail": f"request body exceeds {limit} bytes"},
                    )
        return await call_next(request)
