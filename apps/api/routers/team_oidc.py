"""OIDC single sign-on for team members (``brain.team.oidc``; docs/operations/TEAM_SSO.md).

Every route here is unauthenticated by design: it is how a person without a
credential gets one. The dashboard's ``/auth/oidc/start`` and
``/auth/oidc/callback`` handlers are the intended callers; they keep the
``state`` and a random browser binding secret in an httpOnly cookie and send
the binding with both calls, so a stolen authorization code cannot be redeemed
from another browser. Invitation tokens and codes travel in JSON bodies, never
in this API's URLs. Start and callback share a per-address throttle (socket
peer), kept in Redis like invitation acceptance so every API process counts
against one limit (``apps/api/invitation_throttle.py``; in memory under Lite).
A throttled attempt gets HTTP 429 with ``Retry-After``. Answers are ``no-store``.

* ``GET /team/oidc/config`` - whether SSO is offered (``enabled``) and whether
  invitations require it.
* ``POST /team/oidc/start`` - ``{binding, invitation?}`` -> ``{authorization_url, state, purpose}``.
* ``POST /team/oidc/callback`` - ``{state, code, binding}`` -> ``{mode, credential, member}``;
  ``mode`` is ``signin`` or ``invitation``. 404 when SSO is off, 403 when the
  flow, token or identity is refused, 409 for team conflicts, 502 when the
  identity provider cannot be used.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError

from apps.api.invitation_throttle import AcceptThrottle, InProcessWindow, RedisWindow, retry_after_header
from brain.config.settings import settings

from brain.database.session import async_session_factory
from brain.team import invitations, oidc
from brain.team.authorization import TeamConflict, TeamDenied, require_team_storage
from brain.team.provisioning import TeamNotFound

router = APIRouter(prefix="/team/oidc", tags=["experimental-team"])

OIDC_LIMIT = 30
OIDC_WINDOW_S = 300
OIDC_KEY_PREFIX = "brain:team:oidc:"
_throttle: AcceptThrottle | None = None


class OidcStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    binding: str = Field(min_length=oidc.MIN_BINDING_LENGTH, max_length=oidc.MAX_BINDING_LENGTH)
    invitation: str | None = Field(default=None, max_length=invitations.MAX_TOKEN_LENGTH)


class OidcCallbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    state: str = Field(min_length=16, max_length=256)
    code: str = Field(min_length=1, max_length=2048)
    binding: str = Field(min_length=oidc.MIN_BINDING_LENGTH, max_length=oidc.MAX_BINDING_LENGTH)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client and request.client.host else "unknown"


def oidc_throttle() -> AcceptThrottle:
    """Shared Redis window under full storage (its own key prefix), the in-process window under Lite."""
    global _throttle
    if _throttle is None:
        if settings.BRAIN_STORAGE_MODE == "lite":
            _throttle = InProcessWindow()
        else:
            from brain.database.session import redis_client

            _throttle = RedisWindow(redis_client, prefix=OIDC_KEY_PREFIX)
    return _throttle


async def _check_throttle(request: Request) -> None:
    retry_after = await oidc_throttle().hit(_client_ip(request), limit=OIDC_LIMIT, window_s=OIDC_WINDOW_S)
    if retry_after is not None:
        raise HTTPException(429, "Too many sign-in attempts; try again later",
                            headers={"Retry-After": retry_after_header(retry_after)})


def _refusal(exc: Exception) -> HTTPException:
    if isinstance(exc, oidc.OidcDisabled):
        return HTTPException(404, oidc.DISABLED)
    if isinstance(exc, invitations.InvitationRejected):
        return HTTPException(403, invitations.REJECTED)
    if isinstance(exc, (oidc.OidcRejected, TeamDenied)):
        message = str(exc) if isinstance(exc, oidc.OidcRejected) or str(exc) == invitations.EMAIL_MISMATCH else None
        return HTTPException(403, message or "Team authority denied")
    if isinstance(exc, oidc.OidcUnavailable):
        return HTTPException(502, f"Single sign-on is unavailable: {exc}")
    if isinstance(exc, TeamNotFound):
        return HTTPException(404, str(exc))
    if isinstance(exc, TeamConflict):
        return HTTPException(409, str(exc))
    if isinstance(exc, IntegrityError):
        return HTTPException(409, "Sign-in could not be completed concurrently; start again")
    raise exc


HANDLED = (oidc.OidcDisabled, oidc.OidcUnavailable, TeamDenied, TeamNotFound, TeamConflict, IntegrityError)


@router.get("/config")
async def oidc_config(response: Response) -> dict[str, bool]:
    response.headers["Cache-Control"] = "no-store"
    return oidc.public_status()


@router.post("/start")
async def oidc_start(body: OidcStartRequest, request: Request, response: Response) -> dict:
    if oidc.oidc_config() is None:
        raise HTTPException(404, oidc.DISABLED)
    await _check_throttle(request)
    try:
        require_team_storage()
        async with async_session_factory() as session, session.begin():
            result = await oidc.begin(session, binding=body.binding, invitation_token=body.invitation or None)
    except HANDLED as exc:
        raise _refusal(exc) from exc
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post("/callback")
async def oidc_callback(body: OidcCallbackRequest, request: Request, response: Response) -> dict:
    if oidc.oidc_config() is None:
        raise HTTPException(404, oidc.DISABLED)
    await _check_throttle(request)
    try:
        require_team_storage()
        # 1. Spend the request first and commit, so a code can be tried only once.
        async with async_session_factory() as session, session.begin():
            ticket = await oidc.consume_flow(session, state=body.state, binding=body.binding)
        # 2. Talk to the provider outside any transaction.
        identity = await oidc.authenticate(ticket, body.code)
        # 3. Decide and issue in one transaction.
        async with async_session_factory() as session, session.begin():
            if ticket.purpose == "invitation":
                result = await oidc.accept(session, ticket, identity)
            else:
                result = await oidc.sign_in(session, identity)
    except HANDLED as exc:
        raise _refusal(exc) from exc
    response.headers["Cache-Control"] = "no-store"
    return result
