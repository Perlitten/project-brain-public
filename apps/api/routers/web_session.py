"""Who is signed into the web dashboard (per-member web identity).

The dashboard (apps/web) keeps a team member's named credential in an
encrypted httpOnly cookie and forwards it on every call. Before each request it
asks this route who the credential belongs to, so a revoked credential, a
disabled principal or a revoked membership locks the browser out on the next
request — the same checks every other route applies, nothing cached.

Solo state answers for any authenticated caller (the owner signs in with the
dashboard's own login and the server key, as before). In team state only an
active, verified human team member gets an answer; everyone else gets 403.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from apps.api import team_access
from apps.api.auth import require_principal
from brain.auth.principals import ResolvedPrincipal
from brain.database.session import async_session_factory
from brain.team.authorization import TeamDenied, human_member, live_credential
from brain.team.provisioning import ADMIN_SCOPE, TEAM_ID

router = APIRouter(prefix="/api/web-session", tags=["web"])


@router.get("")
async def web_session(principal: ResolvedPrincipal = Depends(require_principal)) -> dict[str, object]:
    """The caller as the dashboard shows it: name, team state and whether team administration is offered."""
    mode = await team_access.current_team_state()
    identity = {"id": principal.id, "name": principal.name, "kind": principal.kind}
    if mode == "solo":
        return {"mode": mode, "principal": identity, "team_admin": True, "member": None}
    try:
        async with async_session_factory() as session:
            live = await live_credential(session, principal.id, principal.credential_id)
            member = await human_member(session, live, TEAM_ID)
            admin = bool(member.is_admin) and live.has_scope(ADMIN_SCOPE)
            details = {"is_admin": bool(member.is_admin), "verified_at":
                       member.verified_at.isoformat() if member.verified_at else None}
    except TeamDenied as exc:
        raise HTTPException(status_code=403, detail=f"Dashboard sign-in denied: {exc}") from exc
    return {"mode": mode, "principal": identity, "team_admin": admin, "member": details}
