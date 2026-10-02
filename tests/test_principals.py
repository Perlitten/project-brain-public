"""Unit tests for principal resolution, credential minting, and scope checks."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("ENVIRONMENT", "test")

import pytest  # noqa: E402

from brain.database.models import Principal  # noqa: E402
from brain.auth.principals import (  # noqa: E402
    ALL_SCOPES,
    CREDENTIAL_PREFIX,
    ResolvedPrincipal,
    generate_credential,
    hash_credential,
    mint_credential,
    resolve_credential,
    require_scopes,
)


class _FakeResult:
    def __init__(self, row=None, scalar=None):
        self._row = row
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar

    def one_or_none(self):
        return self._row


class _FakeSession:
    """In-memory stand-in covering the select calls used by principals.py."""

    def __init__(self):
        self.principals: dict[int, object] = {}
        self.credentials: dict[str, object] = {}
        self._next_id = 1
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = self._next_id
                self._next_id += 1
            type_name = type(obj).__name__
            if type_name == "Principal":
                self.principals[obj.id] = obj
            elif type_name == "ApiCredential":
                self.credentials[obj.key_hash] = obj
        self.added.clear()

    async def execute(self, stmt):
        # select(Principal).where(name == X) — tests only ever mint one
        # principal name, so returning the first stored row is sufficient.
        entities = {cd.get("entity") for cd in getattr(stmt, "column_descriptions", ())}
        if entities and all(e is Principal for e in entities) and self.principals:
            return _FakeResult(scalar=next(iter(self.principals.values())))
        return _FakeResult()


def _admin() -> ResolvedPrincipal:
    return ResolvedPrincipal(id=0, name="legacy", kind="service")


def _principal(scopes: set[str]) -> ResolvedPrincipal:
    return ResolvedPrincipal(id=7, name="svc", kind="service", scopes=frozenset(scopes))


def test_hash_and_generate_are_deterministic_and_prefixed():
    raw = generate_credential()
    assert raw.startswith(f"{CREDENTIAL_PREFIX}_")
    assert len(hash_credential(raw)) == 64
    assert hash_credential(raw) == hash_credential(raw)


def test_admin_scope_star_satisfies_any_scope():
    assert _admin().has_scope("jobs:write")
    assert require_scopes(_admin(), "jobs:write", "jobs:read")


def test_scoped_principal_checks():
    p = _principal({"jobs:read"})
    assert p.has_scope("jobs:read")
    assert not p.has_scope("jobs:write")
    assert not require_scopes(p, "jobs:write")
    assert require_scopes(p, "jobs:read")
    assert ALL_SCOPES not in p.scopes


@pytest.mark.asyncio
async def test_mint_creates_principal_and_hashes_key():
    session = _FakeSession()
    raw, cred = await mint_credential(
        session, name="svc-1", scopes=["jobs:write"], ttl_days=30
    )
    await session.flush()
    assert cred.key_hash == hash_credential(raw)
    assert CREDENTIAL_PREFIX in raw
    assert cred.expires_at is not None
    assert cred.scopes == ["jobs:write"]


@pytest.mark.asyncio
async def test_mint_reuses_existing_principal():
    session = _FakeSession()
    await mint_credential(session, name="svc-1", scopes=["jobs:write"])
    await session.flush()
    raw2, cred2 = await mint_credential(session, name="svc-1", scopes=["jobs:read"])
    await session.flush()
    assert len(session.principals) == 1
    assert cred2.scopes == ["jobs:read"]
    assert raw2


@pytest.mark.asyncio
async def test_resolve_rejects_revoked_expired_disabled_and_unknown():
    session = _FakeSession()
    raw, cred = await mint_credential(session, name="svc-1", scopes=["jobs:read"])
    await session.flush()

    cred.last_used_at = None

    async def fake_execute(stmt):
        key = hash_credential(raw)
        for stored_hash, c in session.credentials.items():
            if stored_hash == key:
                principal = Principal(
                    name="svc-1", kind="service", disabled_at=None
                )
                return _FakeResult(row=(c, principal))
        return _FakeResult(row=None)

    session.execute = fake_execute

    resolved = await resolve_credential(session, raw)
    assert resolved is not None
    assert resolved.name == "svc-1"
    assert cred.last_used_at is not None

    # revoked
    cred.revoked_at = datetime.now(timezone.utc)
    assert await resolve_credential(session, raw) is None

    # expired
    cred.revoked_at = None
    cred.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert await resolve_credential(session, raw) is None

    # disabled principal
    cred.expires_at = None

    async def fake_execute_disabled(stmt):
        principal = Principal(
            name="svc-1", kind="service",
            disabled_at=datetime.now(timezone.utc),
        )
        return _FakeResult(row=(cred, principal))

    session.execute = fake_execute_disabled
    assert await resolve_credential(session, raw) is None

    # unknown key
    async def fake_execute_none(stmt):
        return _FakeResult(row=None)

    session.execute = fake_execute_none
    assert await resolve_credential(session, "pbk_unknown") is None
