"""Native MCP over Streamable HTTP at ``/mcp``.

Until now a remote agent reached the Brain through ``remote_server.py`` running
on its own machine: a stdio MCP server bridging to this API. This endpoint is
the same server spoken natively, so any spec-compliant client connects with a
URL alone - no Python install, no local process.

* **Same tools.** The tool set is ``remote_server.mcp``. In-process it calls
  this API through an ASGI transport, presenting the MCP caller's own
  credential, so per-endpoint scopes, audit rows and telemetry see the agent
  exactly as they would over the REST API.
* **Same credentials.** ``Authorization: Bearer <key>`` or ``X-API-Key`` with
  any ``pbk_`` credential or the legacy key; with ``BRAIN_PUBLIC_URL`` (or
  ``BRAIN_PUBLIC_HOST``) set, the OAuth flow in ``mcp_oauth.py`` mints those
  credentials for clients that cannot be given a key.
* **Stateful sessions.** Elicitation and sampling are server-to-client
  requests in the middle of a tool call, so sessions are kept; the API runs a
  single uvicorn worker, which is what keeps them reachable. Each session is
  bound to the principal that opened it.
"""

from __future__ import annotations

import contextlib
from typing import Any, AsyncIterator, Optional

from loguru import logger
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser, RequireAuthMiddleware
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.routes import build_resource_metadata_url
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from pydantic import AnyHttpUrl
from starlette.authentication import AuthCredentials, AuthenticationBackend
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from apps.api.auth import DEV_PRINCIPAL, dev_open, resolve_api_key
from apps.mcp_server import remote_server
from brain.config.settings import settings

MCP_PATH = "/mcp"


def public_base_url(public_url: Optional[str], public_host: Optional[str]) -> Optional[str]:
    if public_url and public_url.strip():
        return public_url.strip().rstrip("/")
    if public_host and public_host.strip():
        return f"https://{public_host.strip().rstrip('/')}"
    return None


class _CredentialBackend(AuthenticationBackend):
    """Accept the API's own credentials, as a Bearer token or ``X-API-Key``."""

    async def authenticate(self, conn: HTTPConnection) -> Optional[tuple[AuthCredentials, AuthenticatedUser]]:
        authorization = conn.headers.get("authorization", "")
        token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
        token = token or (conn.headers.get("x-api-key") or "").strip()
        if not token:
            if not dev_open():
                return None
            # Mirrors require_principal: an unconfigured non-production Brain is
            # open, and the empty token makes the in-process call keyless too.
            principal = DEV_PRINCIPAL
        else:
            resolved = await resolve_api_key(token)
            if resolved is None:
                return None
            principal = resolved
        # The audit middleware reads the principal from request state.
        conn.scope.setdefault("state", {})["principal"] = principal
        if settings.BRAIN_TEAM_MODE and principal.id <= 0:
            return None
        scopes = sorted(principal.scopes)
        # Keyed by principal name (not credential) so a session survives an
        # OAuth token refresh while staying closed to every other principal.
        access = AccessToken(token=token, client_id=principal.name, scopes=scopes)
        return AuthCredentials(scopes), AuthenticatedUser(access)


class _SessionManagerHolder:
    """A session manager per API lifespan.

    ``StreamableHTTPSessionManager.run()`` works once per instance, and tests
    start the app's lifespan many times in one process.
    """

    def __init__(self, mcp_server: Any) -> None:
        self._mcp_server = mcp_server
        self.manager: Optional[StreamableHTTPSessionManager] = None

    @contextlib.asynccontextmanager
    async def serving(self) -> AsyncIterator[None]:
        manager = StreamableHTTPSessionManager(app=self._mcp_server, session_idle_timeout=1800)
        async with manager.run():
            self.manager = manager
            try:
                yield
            finally:
                self.manager = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.manager is None:
            response = JSONResponse({"detail": "MCP endpoint is not running"}, status_code=503)
            await response(scope, receive, send)
            return
        await self.manager.handle_request(scope, receive, send)


class McpHttpEndpoint:
    def __init__(self, holder: _SessionManagerHolder, resource_metadata_url: Optional[AnyHttpUrl]) -> None:
        self.holder = holder
        self.team_mode = settings.BRAIN_TEAM_MODE
        self.asgi: ASGIApp = AuthenticationMiddleware(
            AuthContextMiddleware(RequireAuthMiddleware(holder, [], resource_metadata_url)),
            backend=_CredentialBackend(),
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.team_mode != settings.BRAIN_TEAM_MODE:
            await JSONResponse({"detail": "Mode changed; restart the API"}, status_code=503)(scope, receive, send)
            return
        await self.asgi(scope, receive, send)

    def serving(self) -> contextlib.AbstractAsyncContextManager[None]:
        return self.holder.serving()


def install(app: Any, *, public_url: Optional[str]) -> McpHttpEndpoint:
    """Mount ``/mcp`` (and, with a public URL, OAuth) on ``app``.

    The caller enters ``endpoint.serving()`` in the app's lifespan.
    """
    remote_server.use_asgi_backend(app)
    resource_metadata_url = None
    if public_url:
        from apps.api.mcp_oauth import BrainOAuthProvider, consent_router, oauth_routes

        resource = f"{public_url}{MCP_PATH}"
        resource_metadata_url = build_resource_metadata_url(AnyHttpUrl(resource))
        provider = BrainOAuthProvider(public_url)
        app.router.routes.extend(oauth_routes(provider, issuer=public_url, resource=resource))
        app.include_router(consent_router(provider))
        logger.info("MCP OAuth authorization server enabled, issuer {}", public_url)
    selected = remote_server.mcp
    if settings.BRAIN_TEAM_MODE:
        from apps.mcp_server.team_server import mcp as team_mcp
        selected = team_mcp
    endpoint = McpHttpEndpoint(_SessionManagerHolder(selected._lowlevel_server), resource_metadata_url)
    app.router.routes.append(Route(MCP_PATH, endpoint=endpoint, methods=["GET", "POST", "DELETE"]))
    return endpoint
