"""Principal resolution and credential minting.

Core layer for B7 stage 1: replaces the single shared API key with named
principals holding hashed, scoped, revocable credentials. The plaintext key
exists only at mint time — storage and lookup are always by SHA-256.

Transport code (apps/api/auth.py) resolves a presented key into a
``ResolvedPrincipal`` and enforces scopes; org-level isolation lands in a
later stage — ``org_id`` is recorded on the principal now so credentials
already carry their future boundary.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.database.models import ApiCredential, Principal

CREDENTIAL_PREFIX = "pbk"
ALL_SCOPES = "*"


@dataclass(frozen=True)
class ResolvedPrincipal:
    """The attributed caller for a request — synthetic ids mark non-DB

    identities: ``0`` = legacy shared key, ``-1`` = unauthenticated dev mode."""

    id: int
    name: str
    kind: str
    scopes: frozenset[str] = frozenset({ALL_SCOPES})
    org_id: Optional[int] = None
    credential_id: Optional[int] = None

    def has_scope(self, scope: str) -> bool:
        if ALL_SCOPES in self.scopes or scope in self.scopes:
            return True
        # Convention: a write grant implies read on the same domain, so a
        # credential minted as "jobs:write" alone can also satisfy
        # "jobs:read" router-level checks.
        domain, _, action = scope.rpartition(":")
        return action == "read" and bool(domain) and f"{domain}:write" in self.scopes


def hash_credential(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def generate_credential() -> str:
    """A single-use plaintext credential — shown once, stored hashed."""
    return f"{CREDENTIAL_PREFIX}_{secrets.token_urlsafe(32)}"


async def mint_credential(
    session: AsyncSession,
    *,
    name: str,
    scopes: Sequence[str],
    kind: str = "service",
    org_id: Optional[int] = None,
    ttl_days: Optional[int] = None,
) -> tuple[str, ApiCredential]:
    """Create (or reuse) a principal and attach a fresh credential.

    Returns the plaintext key — it is never persisted."""
    res = await session.execute(
        select(Principal).where(Principal.name == name)
    )
    principal = res.scalar_one_or_none()
    if principal is None:
        principal = Principal(name=name, kind=kind, org_id=org_id)
        session.add(principal)
        await session.flush()
    raw = generate_credential()
    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=ttl_days)
        if ttl_days
        else None
    )
    cred = ApiCredential(
        principal_id=principal.id,
        key_hash=hash_credential(raw),
        scopes=list(scopes),
        expires_at=expires_at,
    )
    session.add(cred)
    return raw, cred


async def resolve_credential(
    session: AsyncSession, raw_key: str
) -> Optional[ResolvedPrincipal]:
    """Look up a presented key. Returns None when unknown, revoked, expired,

    or bound to a disabled principal — resolution is fail-closed."""
    res = await session.execute(
        select(ApiCredential, Principal)
        .join(Principal, Principal.id == ApiCredential.principal_id)
        .where(ApiCredential.key_hash == hash_credential(raw_key))
    )
    row = res.one_or_none()
    if row is None:
        return None
    cred, principal = row
    now = datetime.now(timezone.utc)
    if cred.revoked_at is not None:
        return None
    if cred.expires_at is not None and cred.expires_at <= now:
        return None
    if principal.disabled_at is not None:
        return None
    cred.last_used_at = now
    return ResolvedPrincipal(
        id=principal.id,
        name=principal.name,
        kind=principal.kind,
        scopes=frozenset(cred.scopes or []),
        org_id=principal.org_id,
        credential_id=cred.id,
    )


def require_scopes(principal: ResolvedPrincipal, *scopes: str) -> bool:
    return all(principal.has_scope(scope) for scope in scopes)
