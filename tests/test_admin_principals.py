"""Admin router: principal listing, credential mint/revoke, principal

disable/enable over the API — all faked at the session boundary so the
contract (shapes, status codes, hash handling) is pinned without Postgres."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch


with (
    patch("sqlalchemy.ext.asyncio.create_async_engine"),
    patch("redis.asyncio.from_url"),
    patch("neo4j.AsyncGraphDatabase.driver"),
):
    from apps.api.main import app

from fastapi.testclient import TestClient  # noqa: E402

from brain.auth.principals import ResolvedPrincipal  # noqa: E402
from brain.database.models import ApiCredential, Principal  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)


def _principal(**overrides):
    base = {
        "id": 3,
        "name": "ci-indexer",
        "kind": "service",
        "org_id": None,
        "created_at": datetime(2026, 10, 1, tzinfo=timezone.utc),
        "disabled_at": None,
    }
    base.update(overrides)
    return Principal(**base)


def _credential(**overrides):
    base = {
        "id": 11,
        "principal_id": 3,
        "key_hash": "abc123hash",
        "scopes": ["jobs:write"],
        "created_at": datetime(2026, 10, 1, tzinfo=timezone.utc),
        "expires_at": None,
        "revoked_at": None,
        "last_used_at": None,
    }
    base.update(overrides)
    return ApiCredential(**base)


class _Scalars:
    """Supports both the .all() and .one_or_none() call patterns."""

    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def one_or_none(self):
        return self._rows[0] if self._rows else None

    def one(self):
        return self._rows[0]


class _ExecResult:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


class _Begin:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


class _FakeSession:
    """``scalars()`` returns queued result lists in call order; ``execute()``

    returns a single scalar (mint_credential's principal lookup)."""

    def __init__(self, scalars_rows=None, exec_scalar=None):
        self._scalars_rows = list(scalars_rows or [])
        self._exec_scalar = exec_scalar
        self.added = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def begin(self):
        return _Begin()

    async def scalars(self, stmt):
        rows = self._scalars_rows.pop(0) if self._scalars_rows else []
        return _Scalars(rows)

    async def execute(self, stmt):
        return _ExecResult(self._exec_scalar)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass


def _patch_session(session):
    return patch(
        "apps.api.routers.admin.async_session_factory", lambda: session
    )


def test_list_principals_returns_credentials_without_hashes():
    session = _FakeSession(
        scalars_rows=[[_principal()], [_credential()]]
    )
    with _patch_session(session):
        response = client.get("/admin/principals")
    assert response.status_code == 200
    principals = response.json()["principals"]
    assert len(principals) == 1
    assert principals[0]["name"] == "ci-indexer"
    creds = principals[0]["credentials"]
    assert creds[0]["id"] == 11
    assert creds[0]["scopes"] == ["jobs:write"]
    assert "key_hash" not in creds[0]
    assert "abc123hash" not in response.text


def test_mint_credential_returns_plaintext_once_and_stores_hash():
    session = _FakeSession(
        scalars_rows=[[_principal()]], exec_scalar=_principal()
    )
    with _patch_session(session):
        response = client.post(
            "/admin/principals/3/credentials",
            json={"scopes": ["jobs:write", "jobs:read"], "ttl_days": 30},
        )
    assert response.status_code == 201
    body = response.json()
    assert body["api_key"].startswith("pbk_")
    minted = [obj for obj in session.added if isinstance(obj, ApiCredential)]
    assert minted and minted[0].key_hash != body["api_key"]
    assert minted[0].scopes == ["jobs:write", "jobs:read"]
    assert minted[0].expires_at is not None


def test_mint_credential_unknown_principal_404():
    session = _FakeSession(scalars_rows=[[]])
    with _patch_session(session):
        response = client.post(
            "/admin/principals/999/credentials", json={"scopes": ["jobs:read"]}
        )
    assert response.status_code == 404


def test_mint_credential_disabled_principal_409():
    session = _FakeSession(
        scalars_rows=[[_principal(disabled_at=datetime.now(timezone.utc))]]
    )
    with _patch_session(session):
        response = client.post(
            "/admin/principals/3/credentials", json={"scopes": ["jobs:read"]}
        )
    assert response.status_code == 409


def test_revoke_credential_sets_timestamp():
    cred = _credential()
    session = _FakeSession(scalars_rows=[[cred]])
    with _patch_session(session):
        response = client.post("/admin/credentials/11/revoke")
    assert response.status_code == 200
    assert cred.revoked_at is not None
    assert response.json()["credential"]["revoked_at"] is not None


def test_revoke_unknown_credential_404():
    session = _FakeSession(scalars_rows=[[]])
    with _patch_session(session):
        response = client.post("/admin/credentials/999/revoke")
    assert response.status_code == 404


def test_disable_then_enable_principal():
    principal = _principal()
    session = _FakeSession(scalars_rows=[[principal]])
    with _patch_session(session):
        response = client.post("/admin/principals/3/disable")
    assert response.status_code == 200
    assert principal.disabled_at is not None

    session = _FakeSession(scalars_rows=[[principal]])
    with _patch_session(session):
        response = client.post("/admin/principals/3/enable")
    assert response.status_code == 200
    assert principal.disabled_at is None
    assert response.json()["principal"]["disabled_at"] is None


def test_admin_routes_require_principals_scope():
    principal = ResolvedPrincipal(
        id=5,
        name="limited-cred",
        kind="service",
        scopes=frozenset({"jobs:read"}),
    )
    with patch(
        "apps.api.auth.require_principal",
        new=AsyncMock(return_value=principal),
    ):
        response = client.get("/admin/principals")
    assert response.status_code == 403


def test_create_principal_mints_first_key_once():
    created = _principal(id=9, name="claude-laptop", kind="agent")
    session = _FakeSession(scalars_rows=[[], [created]], exec_scalar=None)
    with _patch_session(session):
        response = client.post(
            "/admin/principals",
            json={"name": "claude-laptop", "kind": "agent", "scopes": ["core:write", "jobs:read"]},
        )
    assert response.status_code == 201
    body = response.json()
    assert body["api_key"].startswith("pbk_")
    assert body["principal"]["name"] == "claude-laptop"
    minted = [obj for obj in session.added if isinstance(obj, ApiCredential)]
    assert minted and minted[0].scopes == ["core:write", "jobs:read"]
    assert body["api_key"] not in str(body["principal"])


def test_create_principal_existing_name_409():
    session = _FakeSession(scalars_rows=[[_principal()]])
    with _patch_session(session):
        response = client.post("/admin/principals", json={"name": "ci-indexer", "scopes": ["jobs:read"]})
    assert response.status_code == 409


def test_create_principal_rejects_wildcard_scope():
    response = client.post("/admin/principals", json={"name": "x", "scopes": ["*"]})
    assert response.status_code == 422
