"""Team invitations: admins issue single-use tokens, invitees redeem them for a member credential.

Create, list and revoke need a team administrator (a live admin member with
``team:admin``). A Brain that is still a team of one has no such member, so
the API refuses to issue invitations there: the first acceptance turns the
Brain into a team and retires the shared key, which would lock the inviting
caller out unless it was the invitee. The first administrator is bootstrapped
on the server instead (``brain team invite create --admin`` or ``brain team
member add --admin --mint-key``). Accepting needs no API key: the token is the
credential. It is read from the JSON body, never the URL, so access logs never
carry it. Attempts are throttled per client address (socket peer;
X-Forwarded-For is ignored because no trusted-proxy setting exists). The
counter is in-process: one API process per Brain, behind a proxy it acts as
one shared limit.
"""
from __future__ import annotations

import time
import uuid
from collections import defaultdict, deque
from typing import Annotated, Deque, Dict, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError

from apps.api.auth import require_principal
from brain.auth.principals import ResolvedPrincipal
from brain.database.session import async_session_factory
from brain.team import invitations
from brain.team.authorization import TeamConflict, TeamDenied, require_team_storage, team_administrator, team_state
from brain.team.provisioning import TEAM_ID, TeamNotFound

router = APIRouter(prefix="/team/invitations", tags=["experimental-team"])

ACCEPT_LIMIT = 10
ACCEPT_WINDOW_S = 300
_attempts: Dict[str, Deque[float]] = defaultdict(deque)

ProjectRef = Annotated[str, Field(min_length=1, max_length=128)]


class InvitationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    projects: list[ProjectRef] = Field(default_factory=list, max_length=invitations.MAX_PROJECTS)
    role: Literal["reader", "contributor", "reviewer"] = "contributor"
    is_admin: bool = False
    email: str | None = Field(default=None, max_length=254)
    name: str | None = Field(default=None, max_length=64)
    expires_hours: int = Field(default=invitations.DEFAULT_TTL_HOURS, ge=1, le=invitations.MAX_TTL_HOURS)


class InvitationRevokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revoke_member: bool = False


class InvitationAcceptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    token: str = Field(min_length=8, max_length=invitations.MAX_TOKEN_LENGTH)


async def invitation_session():
    try:
        require_team_storage()
        async with async_session_factory() as session, session.begin():
            yield session
    except TeamDenied as exc:
        raise HTTPException(403, "Team authority denied") from exc
    except TeamNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except TeamConflict as exc:
        raise HTTPException(409, str(exc)) from exc


async def _inviter(session, caller: ResolvedPrincipal) -> tuple[str, int]:
    """Audit label and principal id of an authorized inviter (a team administrator)."""
    if await team_state(session) == "solo":
        raise TeamConflict("This Brain has no team members yet. Bootstrap the first administrator on the server "
                           "(`brain team invite create --admin`, then accept it yourself) so you keep access "
                           "once the shared key is retired")
    member = await team_administrator(session, caller, TEAM_ID)
    return f"principal:{member.principal_id}", member.principal_id


def _client_ip(request: Request) -> str:
    return request.client.host if request.client and request.client.host else "unknown"


def _throttled(ip: str) -> bool:
    now = time.monotonic()
    for key in [key for key, window in _attempts.items() if not window or window[-1] < now - ACCEPT_WINDOW_S]:
        del _attempts[key]
    window = _attempts[ip]
    while window and window[0] < now - ACCEPT_WINDOW_S:
        window.popleft()
    if len(window) >= ACCEPT_LIMIT:
        return True
    window.append(now)
    return False


@router.post("")
async def create_invitation(body: InvitationCreateRequest, response: Response,
                            caller: ResolvedPrincipal = Depends(require_principal),
                            session=Depends(invitation_session)):
    label, principal_id = await _inviter(session, caller)
    token, invitation = await invitations.create_invitation(
        session, invited_by=label, invited_by_principal_id=principal_id, projects=body.projects, role=body.role,
        is_admin=body.is_admin, email=body.email, name=body.name, expires_hours=body.expires_hours)
    response.headers["Cache-Control"] = "no-store"
    return {"invitation": invitation, "token": token}


@router.get("")
async def list_invitations(caller: ResolvedPrincipal = Depends(require_principal),
                           session=Depends(invitation_session)):
    await _inviter(session, caller)
    return await invitations.list_invitations(session)


@router.post("/{invitation_id}/revoke")
async def revoke_invitation(invitation_id: uuid.UUID, body: InvitationRevokeRequest | None = None,
                            caller: ResolvedPrincipal = Depends(require_principal),
                            session=Depends(invitation_session)):
    label, principal_id = await _inviter(session, caller)
    return await invitations.revoke_invitation(
        session, invitation_id, revoked_by=label, revoke_issued_member=bool(body and body.revoke_member),
        protected_principal_id=principal_id)


@router.post("/accept")
async def accept_invitation(body: InvitationAcceptRequest, request: Request, response: Response):
    if _throttled(_client_ip(request)):
        raise HTTPException(429, "Too many invitation attempts; try again later")
    try:
        require_team_storage()
        async with async_session_factory() as session, session.begin():
            result = await invitations.accept_invitation(session, body.token)
            request.state.principal = await invitations.accepted_principal(
                session, uuid.UUID(result["invitation"]["id"])
            )
    except invitations.InvitationRejected as exc:
        raise HTTPException(403, invitations.REJECTED) from exc
    except TeamDenied as exc:
        raise HTTPException(403, "Team authority denied") from exc
    except TeamNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except TeamConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except IntegrityError as exc:
        raise HTTPException(409, "Invitation could not be accepted concurrently; retry with the same token") from exc
    response.headers["Cache-Control"] = "no-store"
    return result
