"""Principal and credential administration (B11 slice).

Until now the only way to manage credentials was the ``mint_api_credential``
script on the host. These routes expose the same operations through the API
itself — listing principals and their credentials, minting, revoking, and
disabling — all attributable and scope-gated so a dedicated
``principals:write`` credential can own identity administration.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal, Optional, Sequence

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api import team_access
from apps.api.auth import require_api_key, require_principal, require_scope
from apps.api.team_access import team_administrator_access
from brain.auth.principals import ResolvedPrincipal, mint_credential
from brain.database.models import ApiCredential, Principal
from brain.database.session import async_session_factory
from brain.team.authorization import fresh
from brain.team.models import Team, TeamMember
from brain.team.provisioning import ADMIN_SCOPE, TEAM_ID

router = APIRouter(prefix="/admin", tags=["admin"],
                   dependencies=[Depends(require_api_key), Depends(team_administrator_access())])


# Dotted actions (team:run.request) are named scopes too: provisioning.member_scopes mints them.
_SCOPE_RE = r"^[a-z_]+:[a-z_]+(\.[a-z_]+)*$"


async def _caller(request: Request) -> ResolvedPrincipal:
    """The principal the router-level auth dependency already resolved (as ``require_scope`` reuses it)."""
    principal: ResolvedPrincipal | None = getattr(request.state, "principal", None)
    if principal is None:
        principal = await require_principal(request, request.headers.get("x-api-key"))
    return principal


CALLER = Depends(_caller)


class MintCredentialRequest(BaseModel):
    """Wildcard access is not mintable over the API — use named scopes."""

    scopes: list[Annotated[str, Field(pattern=_SCOPE_RE)]] = Field(min_length=1, max_length=32)
    kind: str = "service"
    org_id: Optional[int] = None
    ttl_days: Optional[int] = Field(default=None, gt=0)


class CreatePrincipalRequest(BaseModel):
    """A new principal with its first credential in one step. Wildcard
    access is not mintable here — use named scopes."""

    name: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._@-]*$")
    kind: Literal["agent", "service", "human"] = "agent"
    scopes: list[Annotated[str, Field(pattern=_SCOPE_RE)]] = Field(min_length=1, max_length=32)
    ttl_days: Optional[int] = Field(default=None, gt=0, le=3650)


def _serialize_principal(principal: Principal) -> dict[str, object]:
    return {
        "id": principal.id,
        "name": principal.name,
        "kind": principal.kind,
        "org_id": principal.org_id,
        "created_at": principal.created_at.isoformat() if principal.created_at else None,
        "disabled_at": principal.disabled_at.isoformat() if principal.disabled_at else None,
    }


def _serialize_credential(cred: ApiCredential) -> dict[str, object]:
    # key_hash is intentionally never returned.
    return {
        "id": cred.id,
        "principal_id": cred.principal_id,
        "scopes": cred.scopes,
        "created_at": cred.created_at.isoformat() if cred.created_at else None,
        "expires_at": cred.expires_at.isoformat() if cred.expires_at else None,
        "revoked_at": cred.revoked_at.isoformat() if cred.revoked_at else None,
        "last_used_at": cred.last_used_at.isoformat() if cred.last_used_at else None,
    }


def _require_held_scopes(caller: ResolvedPrincipal, scopes: list[str]) -> None:
    """A credential can only hand out authority it holds itself."""
    missing = sorted(scope for scope in set(scopes) if not caller.has_scope(scope))
    if missing:
        raise HTTPException(status_code=403, detail=f"Cannot mint scopes the caller does not hold: {missing}")


async def _team_state_is_team() -> bool:
    return await team_access.current_team_state() == "team"


async def _live_admin_credentials(session: AsyncSession) -> list[tuple[int, int]]:
    """``(principal_id, credential_id)`` pairs that can still administer the team."""
    now = datetime.now(timezone.utc)
    rows = (await session.execute(
        select(TeamMember.principal_id, ApiCredential.id, ApiCredential.scopes, ApiCredential.expires_at)
        .join(Principal, Principal.id == TeamMember.principal_id)
        .join(ApiCredential, ApiCredential.principal_id == TeamMember.principal_id)
        .where(TeamMember.team_id == TEAM_ID, TeamMember.is_admin.is_(True), TeamMember.revoked_at.is_(None),
               TeamMember.verified_at.is_not(None), Principal.kind == "human", Principal.disabled_at.is_(None),
               ApiCredential.revoked_at.is_(None)))).all()
    return [(principal_id, credential_id) for principal_id, credential_id, scopes, expires_at in rows
            if (expires_at is None or expires_at > now) and {"*", ADMIN_SCOPE} & set(scopes or [])]


async def _keep_an_administrator(session: AsyncSession, caller: ResolvedPrincipal, *,
                                 principal_id: int | None = None, credential_id: int | None = None) -> None:
    """Team state: refuse locking yourself out, or removing the last live administrator credential.

    Mirrors the self-revocation guard of ``routers/team.py``. The single team
    row is locked so two administrators cannot disable each other concurrently."""
    if principal_id is not None and principal_id == caller.id:
        raise HTTPException(status_code=409, detail="Administrators cannot disable their own principal")
    if credential_id is not None and credential_id == caller.credential_id:
        raise HTTPException(status_code=409, detail="Administrators cannot revoke the credential they are using")
    await fresh(session, Team, Team.id == TEAM_ID, exclusive=True)
    remaining = [pair for pair in await _live_admin_credentials(session)
                 if pair[0] != principal_id and pair[1] != credential_id]
    if not remaining:
        raise HTTPException(status_code=409,
                            detail="This would leave the team without a live administrator credential")


@router.get("/principals", dependencies=[Depends(require_scope("principals:read"))])
async def list_principals() -> dict[str, object]:
    """All principals with their credentials (hashes never exposed)."""
    async with async_session_factory() as session:
        principals: Sequence[Principal] = (
            await session.scalars(select(Principal).order_by(Principal.id))
        ).all()
        credentials: Sequence[ApiCredential] = (
            await session.scalars(
                select(ApiCredential).order_by(ApiCredential.id)
            )
        ).all()
    by_principal: dict[int, list[dict[str, object]]] = {}
    for cred in credentials:
        by_principal.setdefault(cred.principal_id, []).append(
            _serialize_credential(cred)
        )
    return {
        "principals": [
            {
                **_serialize_principal(principal),
                "credentials": by_principal.get(principal.id, []),
            }
            for principal in principals
        ]
    }


@router.post(
    "/principals",
    dependencies=[Depends(require_scope("principals:write"))],
    status_code=201,
)
async def create_principal(body: CreatePrincipalRequest, caller: ResolvedPrincipal = CALLER) -> dict[str, object]:
    """Create a principal and mint its first credential.

    The plaintext key is returned once — only its hash is persisted. The
    caller can only mint scopes it holds."""
    _require_held_scopes(caller, body.scopes)
    async with async_session_factory() as session:
        async with session.begin():
            existing = (
                await session.scalars(select(Principal).where(Principal.name == body.name))
            ).one_or_none()
            if existing is not None:
                raise HTTPException(
                    status_code=409,
                    detail="A principal with that name already exists — mint a key on it instead",
                )
            raw, cred = await mint_credential(
                session,
                name=body.name,
                scopes=sorted(set(body.scopes)),
                kind=body.kind,
                ttl_days=body.ttl_days,
            )
            await session.flush()
            principal = (
                await session.scalars(select(Principal).where(Principal.name == body.name))
            ).one()
            serialized = {**_serialize_principal(principal), "credentials": [_serialize_credential(cred)]}
    return {"api_key": raw, "principal": serialized}


@router.post(
    "/principals/{principal_id}/credentials",
    dependencies=[Depends(require_scope("principals:write"))],
    status_code=201,
)
async def create_credential(
    principal_id: int, body: MintCredentialRequest, caller: ResolvedPrincipal = CALLER
) -> dict[str, object]:
    """Mint a scoped credential for an existing principal.

    The plaintext key is returned once — only its hash is persisted. The
    caller can only mint scopes it holds, and in team state never for another
    team member: member credentials come from invitations or the operator CLI."""
    _require_held_scopes(caller, body.scopes)
    team = await _team_state_is_team()
    async with async_session_factory() as session:
        async with session.begin():
            res = await session.scalars(
                select(Principal).where(Principal.id == principal_id)
            )
            principal = res.one_or_none()
            if principal is None:
                raise HTTPException(status_code=404, detail="Principal not found")
            if principal.disabled_at is not None:
                raise HTTPException(
                    status_code=409, detail="Principal is disabled"
                )
            if team and principal.id != caller.id and await session.get(TeamMember, principal.id) is not None:
                raise HTTPException(status_code=403,
                                    detail="Cannot mint credentials for another team member")
            raw, cred = await mint_credential(
                session,
                name=principal.name,
                scopes=body.scopes,
                kind=principal.kind,
                org_id=body.org_id if body.org_id is not None else principal.org_id,
                ttl_days=body.ttl_days,
            )
            await session.flush()
            serialized = _serialize_credential(cred)
    return {"api_key": raw, "credential": serialized}


@router.post(
    "/credentials/{credential_id}/revoke",
    dependencies=[Depends(require_scope("principals:write"))],
)
async def revoke_credential(credential_id: int, caller: ResolvedPrincipal = CALLER) -> dict[str, object]:
    """Revoke a credential — resolution denies it immediately."""
    team = await _team_state_is_team()
    async with async_session_factory() as session:
        async with session.begin():
            res = await session.scalars(
                select(ApiCredential).where(ApiCredential.id == credential_id)
            )
            cred = res.one_or_none()
            if cred is None:
                raise HTTPException(status_code=404, detail="Credential not found")
            if team and cred.revoked_at is None:
                await _keep_an_administrator(session, caller, credential_id=cred.id)
            if cred.revoked_at is None:
                cred.revoked_at = datetime.now(timezone.utc)
            serialized = _serialize_credential(cred)
    return {"credential": serialized}


@router.post(
    "/principals/{principal_id}/disable",
    dependencies=[Depends(require_scope("principals:write"))],
)
async def disable_principal(principal_id: int, caller: ResolvedPrincipal = CALLER) -> dict[str, object]:
    """Disable a principal — every credential on it stops resolving."""
    team = await _team_state_is_team()
    async with async_session_factory() as session:
        async with session.begin():
            res = await session.scalars(
                select(Principal).where(Principal.id == principal_id)
            )
            principal = res.one_or_none()
            if principal is None:
                raise HTTPException(status_code=404, detail="Principal not found")
            if team and principal.disabled_at is None:
                await _keep_an_administrator(session, caller, principal_id=principal.id)
            if principal.disabled_at is None:
                principal.disabled_at = datetime.now(timezone.utc)
            serialized = _serialize_principal(principal)
    return {"principal": serialized}


@router.post(
    "/principals/{principal_id}/enable",
    dependencies=[Depends(require_scope("principals:write"))],
)
async def enable_principal(principal_id: int) -> dict[str, object]:
    """Re-enable a disabled principal (revoked credentials stay revoked)."""
    async with async_session_factory() as session:
        async with session.begin():
            res = await session.scalars(
                select(Principal).where(Principal.id == principal_id)
            )
            principal = res.one_or_none()
            if principal is None:
                raise HTTPException(status_code=404, detail="Principal not found")
            principal.disabled_at = None
            serialized = _serialize_principal(principal)
    return {"principal": serialized}
