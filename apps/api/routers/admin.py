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

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from apps.api.auth import require_api_key, require_scope
from brain.auth.principals import mint_credential
from brain.database.models import ApiCredential, Principal
from brain.database.session import async_session_factory

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_api_key)])


class MintCredentialRequest(BaseModel):
    scopes: list[str] = Field(min_length=1)
    kind: str = "service"
    org_id: Optional[int] = None
    ttl_days: Optional[int] = Field(default=None, gt=0)


_SCOPE_RE = r"^[a-z_]+:[a-z_]+$"


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
async def create_principal(body: CreatePrincipalRequest) -> dict[str, object]:
    """Create a principal and mint its first credential.

    The plaintext key is returned once — only its hash is persisted."""
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
    principal_id: int, body: MintCredentialRequest
) -> dict[str, object]:
    """Mint a scoped credential for an existing principal.

    The plaintext key is returned once — only its hash is persisted."""
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
async def revoke_credential(credential_id: int) -> dict[str, object]:
    """Revoke a credential — resolution denies it immediately."""
    async with async_session_factory() as session:
        async with session.begin():
            res = await session.scalars(
                select(ApiCredential).where(ApiCredential.id == credential_id)
            )
            cred = res.one_or_none()
            if cred is None:
                raise HTTPException(status_code=404, detail="Credential not found")
            if cred.revoked_at is None:
                cred.revoked_at = datetime.now(timezone.utc)
            serialized = _serialize_credential(cred)
    return {"credential": serialized}


@router.post(
    "/principals/{principal_id}/disable",
    dependencies=[Depends(require_scope("principals:write"))],
)
async def disable_principal(principal_id: int) -> dict[str, object]:
    """Disable a principal — every credential on it stops resolving."""
    async with async_session_factory() as session:
        async with session.begin():
            res = await session.scalars(
                select(Principal).where(Principal.id == principal_id)
            )
            principal = res.one_or_none()
            if principal is None:
                raise HTTPException(status_code=404, detail="Principal not found")
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
