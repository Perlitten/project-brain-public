"""Pure credential encoding and scope contracts.

Persisted issuance, lookup, expiry, revocation and disabled identities are
covered in test_auth_boundary_contracts with an actual SQLite session. A fake
returning the first principal for every query cannot establish those contracts.
"""

from __future__ import annotations

import hashlib
from brain.auth.principals import (  # noqa: E402
    ALL_SCOPES,
    CREDENTIAL_PREFIX,
    ResolvedPrincipal,
    generate_credential,
    hash_credential,
    require_scopes,
)


def _admin() -> ResolvedPrincipal:
    return ResolvedPrincipal(id=0, name="legacy", kind="service")


def _principal(scopes: set[str]) -> ResolvedPrincipal:
    return ResolvedPrincipal(id=7, name="svc", kind="service", scopes=frozenset(scopes))


def test_hash_and_generate_are_deterministic_and_prefixed():
    raw = generate_credential()
    assert raw.startswith(f"{CREDENTIAL_PREFIX}_")
    assert len(hash_credential(raw)) == 64
    # Independent oracle; a constant 64-character hash must not pass.
    assert hash_credential(raw) == hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert hash_credential(raw) != hash_credential(raw + "different")


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

