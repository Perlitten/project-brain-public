"""Storage for the MCP OAuth authorization server.

OAuth adds no second kind of credential: an access token is an ordinary scoped
``pbk_`` key minted through ``mint_credential`` with a short expiry, so every
endpoint, scope check and audit row treats an OAuth agent exactly like an agent
holding a minted key. What OAuth does need on top is persistent state that
survives a restart - the dynamically registered clients and the refresh tokens
that let a client obtain its next access key - and that lives here.

Short-lived state (pending consents, authorization codes) stays in the transport
layer's memory: losing it on restart only means the user clicks "Allow" again.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from brain.auth.principals import hash_credential, mint_credential
from brain.database.models import ApiCredential, OAuthClient, OAuthRefreshToken

REFRESH_PREFIX = "pbr"
PRINCIPAL_PREFIX = "oauth"


@dataclass(frozen=True)
class IssuedTokens:
    access_token: str
    access_expires_at: datetime
    refresh_token: str
    scopes: list[str]


@dataclass(frozen=True)
class StoredRefresh:
    token: str
    client_id: str
    scopes: list[str]
    subject: Optional[str]
    expires_at: Optional[datetime]


@dataclass(frozen=True)
class StoredAccess:
    token: str
    client_id: str
    scopes: list[str]
    expires_at: Optional[datetime]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    # SQLite (tests) drops tzinfo; Postgres keeps it.
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def principal_name_for(client_id: str, client_name: Optional[str]) -> str:
    """One principal per registered client, readable in audit logs."""
    label = re.sub(r"[^A-Za-z0-9._-]+", "-", (client_name or "client").strip())[:48].strip("-") or "client"
    return f"{PRINCIPAL_PREFIX}:{label}:{client_id[:12]}"


# --- clients -------------------------------------------------------------------


async def save_client(session: AsyncSession, client_id: str, client_info: dict[str, Any]) -> None:
    existing = await session.get(OAuthClient, client_id)
    if existing is None:
        session.add(OAuthClient(client_id=client_id, client_info=client_info))
    else:
        existing.client_info = client_info


async def load_client(session: AsyncSession, client_id: str) -> Optional[dict[str, Any]]:
    row = await session.get(OAuthClient, client_id)
    return dict(row.client_info) if row is not None else None


# --- tokens ----------------------------------------------------------------------


async def issue_tokens(
    session: AsyncSession,
    *,
    client_id: str,
    client_name: Optional[str],
    scopes: Sequence[str],
    subject: Optional[str],
    access_ttl: timedelta,
    refresh_ttl: Optional[timedelta],
) -> IssuedTokens:
    """Mint an access credential and the refresh token that can replace it."""
    access_raw, credential = await mint_credential(
        session, name=principal_name_for(client_id, client_name), scopes=list(scopes), kind="service"
    )
    now = _now()
    credential.expires_at = now + access_ttl
    await session.flush()
    refresh_raw = f"{REFRESH_PREFIX}_{secrets.token_urlsafe(32)}"
    session.add(
        OAuthRefreshToken(
            token_hash=hash_credential(refresh_raw),
            client_id=client_id,
            principal_name=principal_name_for(client_id, client_name),
            subject=subject,
            scopes=list(scopes),
            access_credential_id=credential.id,
            expires_at=now + refresh_ttl if refresh_ttl else None,
        )
    )
    return IssuedTokens(
        access_token=access_raw,
        access_expires_at=credential.expires_at,
        refresh_token=refresh_raw,
        scopes=list(scopes),
    )


async def _live_refresh(session: AsyncSession, raw: str) -> Optional[OAuthRefreshToken]:
    row = (
        await session.execute(select(OAuthRefreshToken).where(OAuthRefreshToken.token_hash == hash_credential(raw)))
    ).scalar_one_or_none()
    if row is None or row.revoked_at is not None:
        return None
    expires_at = _aware(row.expires_at)
    if expires_at is not None and expires_at <= _now():
        return None
    return row


async def load_refresh(session: AsyncSession, raw: str) -> Optional[StoredRefresh]:
    row = await _live_refresh(session, raw)
    if row is None:
        return None
    return StoredRefresh(
        token=raw,
        client_id=row.client_id,
        scopes=list(row.scopes or []),
        subject=row.subject,
        expires_at=_aware(row.expires_at),
    )


async def load_access(session: AsyncSession, raw: str) -> Optional[StoredAccess]:
    """An OAuth-issued access credential with the client it was issued to."""
    row = (
        await session.execute(
            select(ApiCredential, OAuthRefreshToken.client_id)
            .join(OAuthRefreshToken, OAuthRefreshToken.access_credential_id == ApiCredential.id)
            .where(ApiCredential.key_hash == hash_credential(raw))
        )
    ).first()
    if row is None:
        return None
    credential, client_id = row
    expires_at = _aware(credential.expires_at)
    if credential.revoked_at is not None or (expires_at is not None and expires_at <= _now()):
        return None
    return StoredAccess(token=raw, client_id=client_id, scopes=list(credential.scopes or []), expires_at=expires_at)


async def _revoke_row(session: AsyncSession, row: OAuthRefreshToken) -> None:
    now = _now()
    row.revoked_at = now
    if row.access_credential_id is not None:
        await session.execute(
            update(ApiCredential)
            .where(ApiCredential.id == row.access_credential_id, ApiCredential.revoked_at.is_(None))
            .values(revoked_at=now)
        )


async def rotate_refresh(
    session: AsyncSession,
    raw: str,
    *,
    client_name: Optional[str],
    scopes: Sequence[str],
    access_ttl: timedelta,
    refresh_ttl: Optional[timedelta],
) -> Optional[IssuedTokens]:
    """Spend a refresh token: revoke it and its access key, issue the next pair."""
    row = await _live_refresh(session, raw)
    if row is None:
        return None
    await _revoke_row(session, row)
    return await issue_tokens(
        session,
        client_id=row.client_id,
        client_name=client_name,
        scopes=scopes,
        subject=row.subject,
        access_ttl=access_ttl,
        refresh_ttl=refresh_ttl,
    )


async def revoke(session: AsyncSession, raw: str) -> None:
    """Revoke a refresh or access token together with its counterpart (RFC 7009)."""
    row = await _live_refresh(session, raw)
    if row is None:
        row = (
            await session.execute(
                select(OAuthRefreshToken)
                .join(ApiCredential, OAuthRefreshToken.access_credential_id == ApiCredential.id)
                .where(ApiCredential.key_hash == hash_credential(raw), OAuthRefreshToken.revoked_at.is_(None))
            )
        ).scalar_one_or_none()
    if row is not None:
        await _revoke_row(session, row)
