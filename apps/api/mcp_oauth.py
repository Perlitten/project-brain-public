"""OAuth 2.1 authorization server for the native MCP endpoint.

MCP clients that cannot be handed an API key (claude.ai connectors, IDEs that
follow the spec's auth discovery) find this server through the protected
resource metadata that ``/mcp`` advertises, register themselves (RFC 7591),
send the user here to approve, and exchange a PKCE-protected code for tokens.

The issued access token is a plain scoped ``pbk_`` credential with a one-hour
expiry (see ``brain/auth/oauth_store.py``), so nothing downstream needs to know
OAuth exists. The human approving the request proves who they are with the web
dashboard sign-in, or with an API key that already holds the requested scopes -
a client can never obtain more than its approver could do directly.
"""

from __future__ import annotations

import html
import secrets
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.handlers.metadata import MetadataHandler
from mcp.server.auth.routes import build_metadata, cors_middleware, create_auth_routes, create_protected_resource_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl
from starlette.routing import Route

from apps.api.auth import dev_open, resolve_api_key
from apps.api.routers.web_login import _configured as web_login_configured
from apps.api.routers.web_login import _fail, _matches, _throttled
from brain.auth import oauth_store
from brain.database.session import async_session_factory

SUPPORTED_SCOPES = ["core:read", "core:write", "jobs:read", "harness:write"]
# What the MCP tools need: core:write covers context/search/impact/review and
# memory writes (write implies read), jobs:read polls background jobs,
# harness:write records lessons and observations.
DEFAULT_SCOPES = ["core:write", "jobs:read", "harness:write"]
ACCESS_TTL = timedelta(hours=1)
REFRESH_TTL = timedelta(days=30)
PENDING_TTL_S = 600
CODE_TTL_S = 300
CONSENT_PATH = "/oauth/consent"


@dataclass
class _Pending:
    client: OAuthClientInformationFull
    params: AuthorizationParams
    scopes: list[str]
    created: float


class BrainOAuthProvider:
    """``OAuthAuthorizationServerProvider`` backed by Brain principals."""

    def __init__(self, issuer: str) -> None:
        self.issuer = issuer.rstrip("/")
        self._pending: dict[str, _Pending] = {}
        self._codes: dict[str, AuthorizationCode] = {}

    def _sweep(self) -> None:
        now = time.time()
        self._pending = {k: v for k, v in self._pending.items() if now - v.created < PENDING_TTL_S}
        self._codes = {k: v for k, v in self._codes.items() if v.expires_at > now}

    # --- clients ---------------------------------------------------------------

    async def get_client(self, client_id: str) -> Optional[OAuthClientInformationFull]:
        async with async_session_factory() as session:
            info = await oauth_store.load_client(session, client_id)
        return OAuthClientInformationFull.model_validate(info) if info else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        assert client_info.client_id is not None
        async with async_session_factory() as session:
            async with session.begin():
                await oauth_store.save_client(
                    session, client_info.client_id, client_info.model_dump(mode="json", exclude_none=True)
                )

    # --- authorization -----------------------------------------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        self._sweep()
        registered = client.scope.split() if client.scope else list(SUPPORTED_SCOPES)
        # No scope asked for: what the tools need, within what the client registered.
        scopes = params.scopes or [scope for scope in DEFAULT_SCOPES if scope in registered] or registered
        request_id = secrets.token_urlsafe(24)
        self._pending[request_id] = _Pending(client=client, params=params, scopes=scopes, created=time.time())
        return f"{self.issuer}{CONSENT_PATH}?request={request_id}"

    def pending(self, request_id: str) -> Optional[_Pending]:
        self._sweep()
        return self._pending.get(request_id)

    def approve(self, request_id: str, subject: str) -> str:
        pending = self._pending.pop(request_id)
        code = secrets.token_urlsafe(32)
        self._codes[code] = AuthorizationCode(
            code=code,
            scopes=pending.scopes,
            expires_at=time.time() + CODE_TTL_S,
            client_id=pending.client.client_id or "",
            code_challenge=pending.params.code_challenge,
            redirect_uri=pending.params.redirect_uri,
            redirect_uri_provided_explicitly=pending.params.redirect_uri_provided_explicitly,
            resource=pending.params.resource,
            subject=subject,
        )
        return construct_redirect_uri(str(pending.params.redirect_uri), code=code, state=pending.params.state)

    def deny(self, request_id: str) -> str:
        pending = self._pending.pop(request_id)
        return construct_redirect_uri(
            str(pending.params.redirect_uri),
            error="access_denied",
            error_description="The user denied the request",
            state=pending.params.state,
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> Optional[AuthorizationCode]:
        self._sweep()
        code = self._codes.get(authorization_code)
        return code if code is not None and code.client_id == client.client_id else None

    # --- tokens --------------------------------------------------------------------

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        # Single use: a replayed code finds nothing.
        if self._codes.pop(authorization_code.code, None) is None:
            raise TokenError(error="invalid_grant", error_description="authorization code already used")
        async with async_session_factory() as session:
            async with session.begin():
                issued = await oauth_store.issue_tokens(
                    session,
                    client_id=client.client_id or "",
                    client_name=client.client_name,
                    scopes=authorization_code.scopes,
                    subject=authorization_code.subject,
                    access_ttl=ACCESS_TTL,
                    refresh_ttl=REFRESH_TTL,
                )
        return _token_response(issued)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> Optional[RefreshToken]:
        async with async_session_factory() as session:
            stored = await oauth_store.load_refresh(session, refresh_token)
        if stored is None or stored.client_id != client.client_id:
            return None
        return RefreshToken(
            token=stored.token,
            client_id=stored.client_id,
            scopes=stored.scopes,
            expires_at=int(stored.expires_at.timestamp()) if stored.expires_at else None,
            subject=stored.subject,
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        async with async_session_factory() as session:
            async with session.begin():
                issued = await oauth_store.rotate_refresh(
                    session,
                    refresh_token.token,
                    client_name=client.client_name,
                    scopes=scopes or refresh_token.scopes,
                    access_ttl=ACCESS_TTL,
                    refresh_ttl=REFRESH_TTL,
                )
        if issued is None:
            raise TokenError(error="invalid_grant", error_description="refresh token is no longer valid")
        return _token_response(issued)

    async def load_access_token(self, token: str) -> Optional[AccessToken]:
        async with async_session_factory() as session:
            stored = await oauth_store.load_access(session, token)
        if stored is None:
            return None
        return AccessToken(
            token=stored.token,
            client_id=stored.client_id,
            scopes=stored.scopes,
            expires_at=int(stored.expires_at.timestamp()) if stored.expires_at else None,
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        async with async_session_factory() as session:
            async with session.begin():
                await oauth_store.revoke(session, token.token)


def _token_response(issued: oauth_store.IssuedTokens) -> OAuthToken:
    return OAuthToken(
        access_token=issued.access_token,
        token_type="Bearer",
        expires_in=int(ACCESS_TTL.total_seconds()),
        scope=" ".join(issued.scopes),
        refresh_token=issued.refresh_token,
    )


# --- consent page ------------------------------------------------------------------


def _consent_page(request_id: str, pending: _Pending, error: str = "") -> str:
    client = html.escape(pending.client.client_name or pending.client.client_id or "An MCP client")
    redirect_host = html.escape(urlparse(str(pending.params.redirect_uri)).netloc or str(pending.params.redirect_uri))
    scopes = "".join(f"<li><code>{html.escape(scope)}</code></li>" for scope in pending.scopes)
    login_fields = (
        '<label>Username <input name="user" autocomplete="username"></label>'
        '<label>Password <input name="password" type="password" autocomplete="current-password"></label>'
        "<p class=or>or</p>"
        if web_login_configured()
        else ""
    )
    if dev_open():
        login_fields = "<p>This Brain has no API key configured, so no sign-in is needed.</p>"
        key_field = ""
    else:
        key_field = (
            '<label>API key with these scopes <input name="api_key" type="password" autocomplete="off"></label>'
        )
    alert = f'<p class=error role=alert>{html.escape(error)}</p>' if error else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Authorize MCP client</title>
<style>
:root{{color-scheme:light dark;--fg:#1a1a1a;--bg:#fafafa;--card:#fff;--line:#d9d9d9;--accent:#2f5bd3;--err:#b3261e}}
@media (prefers-color-scheme:dark){{:root{{--fg:#eee;--bg:#141414;--card:#1e1e1e;--line:#3a3a3a;--accent:#8aa8ff;--err:#ff8a80}}}}
body{{margin:0;font:15px/1.5 system-ui,sans-serif;background:var(--bg);color:var(--fg)}}
main{{max-width:440px;margin:48px auto;padding:24px;background:var(--card);border:1px solid var(--line);border-radius:10px}}
h1{{font-size:20px;margin:0 0 12px}} label{{display:block;margin:10px 0}}
input{{display:block;width:100%;box-sizing:border-box;padding:8px;margin-top:4px;border:1px solid var(--line);
border-radius:6px;background:transparent;color:inherit}}
.actions{{display:flex;gap:8px;margin-top:16px}} button{{flex:1;padding:10px;border-radius:6px;border:1px solid var(--line);
background:transparent;color:inherit;font:inherit;cursor:pointer}} button[value=allow]{{background:var(--accent);
color:#fff;border-color:var(--accent)}} .error{{color:var(--err)}} .or{{text-align:center;margin:4px 0;opacity:.7}}
@media (max-width:480px){{main{{margin:16px;padding:16px}}}}
</style></head><body><main>
<h1>Authorize {client}</h1>
<p><strong>{client}</strong> wants to use this Project Brain over MCP and will return to <code>{redirect_host}</code>.</p>
<p>It is asking for:</p><ul>{scopes}</ul>
{alert}
<form method="post" action="{CONSENT_PATH}">
<input type="hidden" name="request" value="{html.escape(request_id)}">
{login_fields}{key_field}
<div class=actions><button name="action" value="deny">Deny</button>
<button name="action" value="allow">Allow</button></div>
</form></main></body></html>"""


async def _approver(user: str, password: str, api_key: str, scopes: list[str], client_ip: str) -> tuple[str, str]:
    """Return ``(subject, "")`` for an approver who may grant ``scopes``, else ``("", error)``."""
    if dev_open():
        return "dev-open", ""
    if user and web_login_configured():
        if _throttled(user):
            return "", "Too many failed attempts. Try again in a few minutes."
        if await _matches(user, password):
            return f"web:{user}", ""
        _fail(user)
        return "", "Wrong username or password."
    if api_key:
        throttle_key = f"oauth-key:{client_ip}"
        if _throttled(throttle_key):
            return "", "Too many failed attempts. Try again in a few minutes."
        principal = await resolve_api_key(api_key)
        if principal is None:
            _fail(throttle_key)
            return "", "That API key is not valid."
        missing = [scope for scope in scopes if not principal.has_scope(scope)]
        if missing:
            return "", f"That API key cannot grant {', '.join(missing)}."
        return principal.name, ""
    return "", "Sign in or enter an API key to approve."


def consent_router(provider: BrainOAuthProvider) -> APIRouter:
    router = APIRouter(include_in_schema=False)

    @router.get(CONSENT_PATH)
    async def consent_form(request: str = "") -> Response:
        pending = provider.pending(request)
        if pending is None:
            return HTMLResponse("<p>This authorization request has expired. Start again from your MCP client.</p>", 400)
        return HTMLResponse(_consent_page(request, pending), headers={"Cache-Control": "no-store"})

    @router.post(CONSENT_PATH)
    async def consent_submit(
        http_request: Request,
        request: str = Form(""),
        action: str = Form("deny"),
        user: str = Form(""),
        password: str = Form(""),
        api_key: str = Form(""),
    ) -> Response:
        pending = provider.pending(request)
        if pending is None:
            return HTMLResponse("<p>This authorization request has expired. Start again from your MCP client.</p>", 400)
        if action != "allow":
            return RedirectResponse(provider.deny(request), status_code=302)
        client_ip = http_request.client.host if http_request.client else "unknown"
        subject, error = await _approver(user.strip(), password, api_key.strip(), pending.scopes, client_ip)
        if error:
            return HTMLResponse(
                _consent_page(request, pending, error), status_code=401, headers={"Cache-Control": "no-store"}
            )
        return RedirectResponse(provider.approve(request, subject), status_code=302)

    return router


def oauth_routes(provider: BrainOAuthProvider, *, issuer: str, resource: str) -> list:
    """Authorization server and protected-resource metadata routes."""
    routes = create_auth_routes(
        provider,
        issuer_url=AnyHttpUrl(issuer),
        client_registration_options=ClientRegistrationOptions(
            # A client registering without a scope may later ask for any of
            # them; the approver's own scopes still cap what it is granted.
            enabled=True, valid_scopes=SUPPORTED_SCOPES, default_scopes=SUPPORTED_SCOPES
        ),
        revocation_options=RevocationOptions(enabled=True),
    )
    # The SDK's v1 default omits public clients, which Brain registers with
    # token_endpoint_auth_method=none. Preserve the other advertised methods.
    metadata = build_metadata(
        AnyHttpUrl(issuer), None,
        ClientRegistrationOptions(enabled=True, valid_scopes=SUPPORTED_SCOPES, default_scopes=SUPPORTED_SCOPES),
        RevocationOptions(enabled=True),
    )
    metadata.token_endpoint_auth_methods_supported = ["none", "client_secret_post", "client_secret_basic"]
    metadata.revocation_endpoint_auth_methods_supported = ["none", "client_secret_post", "client_secret_basic"]
    routes = [route for route in routes if route.path != "/.well-known/oauth-authorization-server"]
    routes.insert(0, Route(
        "/.well-known/oauth-authorization-server",
        endpoint=cors_middleware(MetadataHandler(metadata).handle, ["GET", "OPTIONS"]),
        methods=["GET", "OPTIONS"],
    ))
    routes += create_protected_resource_routes(
        resource_url=AnyHttpUrl(resource),
        authorization_servers=[AnyHttpUrl(issuer)],
        scopes_supported=SUPPORTED_SCOPES,
        resource_name="Project Brain",
    )
    return routes
