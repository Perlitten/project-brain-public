"""API key authentication and principal resolution for orchestration endpoints.

Every request that presents a key is attributed to a ``ResolvedPrincipal`` on
``request.state.principal``:

- ``pbk_...`` keys resolve through ``api_credentials`` (hashed lookup,
  fail-closed on revoked/expired/disabled);
- the legacy ``PROJECT_BRAIN_API_KEY`` resolves to a synthetic admin
  principal so existing deployments keep working until credentials are
  minted;
- no key: production fails closed (503), local/dev resolves to a synthetic
  open principal.

``require_scope`` then enforces per-endpoint scopes on the attributed
principal.
"""

import hmac

from fastapi import Header, HTTPException, Request

from brain.auth.principals import (
    ResolvedPrincipal,
    resolve_credential,
)
from brain.config.settings import settings
from brain.database.session import async_session_factory

LEGACY_PRINCIPAL = ResolvedPrincipal(
    id=0,
    name="legacy-api-key",
    kind="service",
)
DEV_PRINCIPAL = ResolvedPrincipal(
    id=-1,
    name="dev-open",
    kind="service",
)


async def require_principal(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> ResolvedPrincipal:
    if not x_api_key:
        if settings.PROJECT_BRAIN_API_KEY:
            raise HTTPException(status_code=401, detail="Invalid or missing API key")
        if settings.ENVIRONMENT.lower() == "production":
            # Fail closed in production: an unconfigured key must never
            # silently leave the mutating API open.
            raise HTTPException(status_code=503, detail="API key not configured on server")
        principal = DEV_PRINCIPAL
    elif hmac.compare_digest(x_api_key, settings.PROJECT_BRAIN_API_KEY or ""):
        principal = LEGACY_PRINCIPAL
    else:
        async with async_session_factory() as session:
            async with session.begin():
                resolved = await resolve_credential(session, x_api_key)
        if resolved is None:
            raise HTTPException(status_code=401, detail="Invalid or missing API key")
        principal = resolved
    request.state.principal = principal
    return principal


async def require_api_key(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> ResolvedPrincipal:
    """Back-compat alias: attributes the request and enforces a presented key."""
    return await require_principal(request, x_api_key)


def require_scope(*scopes: str):
    """Endpoint dependency enforcing scope(s) on the resolved principal.

    Self-sufficient: when the request was not attributed yet (e.g. the
    router-level auth dependency was bypassed or reordered), it resolves the
    presented key itself via ``require_principal`` — fail-closed exactly as
    the router-level check, never silently open."""

    async def _checker(request: Request) -> None:
        principal: ResolvedPrincipal | None = getattr(
            request.state, "principal", None
        )
        if principal is None:
            principal = await require_principal(
                request, request.headers.get("x-api-key")
            )
        if not all(principal.has_scope(scope) for scope in scopes):
            raise HTTPException(
                status_code=403,
                detail=f"Principal '{principal.name}' lacks required scope",
            )

    return _checker
