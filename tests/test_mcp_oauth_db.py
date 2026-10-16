"""OAuth 2.1 for ``/mcp`` against Postgres: registration, consent, PKCE code
exchange, refresh rotation and revocation, with the issued tokens used on /mcp.

Skipped locally without Postgres; required under GITHUB_ACTIONS (same contract
as test_memory_consolidation_db).
"""

import asyncio
import base64
import contextlib
import hashlib
import os
import secrets
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from apps.api import mcp_http
from apps.mcp_server import remote_server
from brain.auth.principals import hash_credential, mint_credential
from brain.config.settings import settings
from brain.database.models import ApiCredential, OAuthClient, OAuthRefreshToken, Principal
from brain.team.models import TeamAccessFence

_TABLES = [model.__table__ for model in (Principal, ApiCredential, OAuthClient, OAuthRefreshToken, TeamAccessFence)]

ISSUER = "http://localhost"
REDIRECT = "http://127.0.0.1:9999/callback"
LEGACY_KEY = "legacy-test-key"
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
}
STREAM_HEADERS = {"Accept": "application/json, text/event-stream"}


@pytest_asyncio.fixture
async def sessions(monkeypatch):
    engine = create_async_engine(settings.DATABASE_URL, connect_args={
        "server_settings": {"application_name": f"brain-oauth-tests-{secrets.token_hex(6)}"},
    })
    try:
        async with engine.begin() as conn:
            # Only the auth tables: every test registers fresh clients, so no truncation.
            await conn.run_sync(lambda sync_conn: Principal.metadata.create_all(sync_conn, tables=_TABLES))
    except (OSError, ConnectionError):
        await engine.dispose()
        if os.environ.get("GITHUB_ACTIONS"):
            raise
        pytest.skip("PostgreSQL is unavailable locally")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("apps.api.mcp_oauth.async_session_factory", factory)
    monkeypatch.setattr("apps.api.auth.async_session_factory", factory)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_API_KEY", LEGACY_KEY)
    monkeypatch.setattr(remote_server, "_asgi_app", None)
    yield factory
    await engine.dispose()


@pytest.fixture
def oauth_app(sessions):
    app = FastAPI()
    return app, mcp_http.install(app, public_url=ISSUER)


@contextlib.asynccontextmanager
async def _client(oauth_app):
    # Entered inside the test: the session manager's task group must be exited
    # by the task that entered it, which an async fixture's teardown is not.
    app, endpoint = oauth_app
    async with endpoint.serving():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ISSUER) as http:
            yield http


async def _register(http, name="Test Client"):
    response = await http.post(
        "/register",
        json={
            "redirect_uris": [REDIRECT],
            "client_name": name,
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["client_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["client_secret_post", "client_secret_basic"])
async def test_confidential_revocation_still_requires_correct_secret(oauth_app, method):
    async with _client(oauth_app) as client:
        response = await client.post("/register", json={
            "redirect_uris": [REDIRECT], "client_name": "Confidential fixture",
            "token_endpoint_auth_method": method, "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        })
        assert response.status_code == 201, response.text
        registered = response.json()
        client_id, secret = registered["client_id"], registered["client_secret"]
        data = {"token": "unknown-fixture-token", "client_id": client_id}
        assert (await client.post("/revoke", data=data)).status_code == 401
        if method == "client_secret_post":
            assert (await client.post("/revoke", data={**data, "client_secret": "incorrect"})).status_code == 401
            correct = await client.post("/revoke", data={**data, "client_secret": secret})
        else:
            def header(value):
                return {"Authorization": "Basic " + base64.b64encode(f"{client_id}:{value}".encode()).decode()}
            assert (await client.post("/revoke", data=data, headers=header("incorrect"))).status_code == 401
            correct = await client.post("/revoke", data=data, headers=header(secret))
        assert correct.status_code == 200
        assert correct.headers["cache-control"] == "no-store"


def _pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


async def _authorize(http, client_id, challenge, scope=None):
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "st4te",
    }
    if scope:
        params["scope"] = scope
    response = await http.get("/authorize", params=params)
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert location.startswith(f"{ISSUER}/oauth/consent?request=")
    return parse_qs(urlparse(location).query)["request"][0]


async def _consent(http, request_id, **form):
    return await http.post("/oauth/consent", data={"request": request_id, **form})


async def _mcp_status(http, token):
    response = await http.post("/mcp", json=INIT, headers={**STREAM_HEADERS, "Authorization": f"Bearer {token}"})
    return response.status_code


@pytest.mark.asyncio
async def test_full_flow_issues_rotates_and_revokes_tokens(oauth_app):
    async with _client(oauth_app) as client:
        client_id = await _register(client)
        verifier, challenge = _pkce()
        request_id = await _authorize(client, client_id, challenge)

        page = await client.get("/oauth/consent", params={"request": request_id})
        assert page.status_code == 200
        assert "Test Client" in page.text
        assert "127.0.0.1:9999" in page.text
        assert "core:write" in page.text

        refused = await _consent(client, request_id, action="allow", api_key="pbk_not_a_key")
        assert refused.status_code == 401
        assert "not valid" in refused.text

        approved = await _consent(client, request_id, action="allow", api_key=LEGACY_KEY)
        assert approved.status_code == 302
        query = parse_qs(urlparse(approved.headers["location"]).query)
        assert query["state"] == ["st4te"]
        code = query["code"][0]

        wrong_verifier = await client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT,
                "client_id": client_id,
                "code_verifier": secrets.token_urlsafe(48),
            },
        )
        assert wrong_verifier.status_code == 400

        exchanged = await client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT,
                "client_id": client_id,
                "code_verifier": verifier,
            },
        )
        assert exchanged.status_code == 200, exchanged.text
        tokens = exchanged.json()
        assert tokens["access_token"].startswith("pbk_")
        assert tokens["refresh_token"].startswith("pbr_")
        assert set(tokens["scope"].split()) == {"core:write", "jobs:read", "harness:write"}

        replay = await client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT,
                "client_id": client_id,
                "code_verifier": verifier,
            },
        )
        assert replay.status_code == 400

        assert await _mcp_status(client, tokens["access_token"]) == 200

        refreshed = await client.post(
            "/token",
            data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client_id},
        )
        assert refreshed.status_code == 200, refreshed.text
        rotated = refreshed.json()
        assert rotated["access_token"] != tokens["access_token"]
        # Rotation spends the old pair.
        assert await _mcp_status(client, tokens["access_token"]) == 401
        reused = await client.post(
            "/token",
            data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client_id},
        )
        assert reused.status_code == 400
        assert await _mcp_status(client, rotated["access_token"]) == 200

        revoked = await client.post("/revoke", data={"token": rotated["refresh_token"], "client_id": client_id})
        assert revoked.status_code == 200, revoked.text
        assert await _mcp_status(client, rotated["access_token"]) == 401


@pytest.mark.asyncio
async def test_issued_token_carries_the_granted_scopes(oauth_app):
    async with _client(oauth_app) as client:
        from apps.api.auth import resolve_api_key

        client_id = await _register(client, name="Reader")
        verifier, challenge = _pkce()
        request_id = await _authorize(client, client_id, challenge, scope="core:read")
        approved = await _consent(client, request_id, action="allow", api_key=LEGACY_KEY)
        code = parse_qs(urlparse(approved.headers["location"]).query)["code"][0]
        tokens = (
            await client.post(
                "/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": REDIRECT,
                    "client_id": client_id,
                    "code_verifier": verifier,
                },
            )
        ).json()
        principal = await resolve_api_key(tokens["access_token"])
        assert principal is not None
        assert principal.name.startswith("oauth:Reader:")
        assert principal.kind == "service"
        assert principal.has_scope("core:read")
        assert not principal.has_scope("core:write")


@pytest.mark.asyncio
async def test_approver_cannot_grant_more_than_their_key_holds(oauth_app, sessions):
    async with _client(oauth_app) as client:
        async with sessions() as session:
            async with session.begin():
                reader_key, _ = await mint_credential(session, name=f"reader-{secrets.token_hex(4)}", scopes=["core:read"])
        client_id = await _register(client)
        _, challenge = _pkce()
        request_id = await _authorize(client, client_id, challenge)
        refused = await _consent(client, request_id, action="allow", api_key=reader_key)
        assert refused.status_code == 401
        assert "cannot grant" in refused.text


@pytest.mark.asyncio
async def test_denied_consent_returns_access_denied(oauth_app):
    async with _client(oauth_app) as client:
        client_id = await _register(client)
        _, challenge = _pkce()
        request_id = await _authorize(client, client_id, challenge)
        denied = await _consent(client, request_id, action="deny")
        assert denied.status_code == 302
        query = parse_qs(urlparse(denied.headers["location"]).query)
        assert query["error"] == ["access_denied"]
        assert query["state"] == ["st4te"]
        # The request is spent: it cannot be approved afterwards.
        assert (await _consent(client, request_id, action="allow", api_key=LEGACY_KEY)).status_code == 400


@pytest.mark.asyncio
async def test_refresh_rotation_is_single_use_under_overlap(oauth_app, sessions):
    async with _client(oauth_app) as client:
        client_id = await _register(client)
        verifier, challenge = _pkce()
        request_id = await _authorize(client, client_id, challenge)
        approved = await _consent(client, request_id, action="allow", api_key=LEGACY_KEY)
        code = parse_qs(urlparse(approved.headers["location"]).query)["code"][0]
        exchanged = await client.post("/token", data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
            "client_id": client_id, "code_verifier": verifier,
        })
        tokens = exchanged.json()
        data = {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client_id}
        requests = []
        try:
            # Hold the persisted refresh row so both HTTP requests overlap. With
            # proper serialization they wait before spending it. Without it,
            # both read live state and reach conflicting writes before release.
            async with sessions() as blocker, blocker.begin():
                app_name = await blocker.scalar(text("SELECT current_setting('application_name')"))
                await blocker.execute(select(OAuthRefreshToken).where(
                    OAuthRefreshToken.token_hash == hash_credential(tokens["refresh_token"]),
                ).with_for_update())
                requests = [asyncio.create_task(client.post("/token", data=data)) for _ in range(2)]

                async def both_waiting():
                    while True:
                        assert not any(request.done() for request in requests)
                        async with sessions() as observer:
                            count = await observer.scalar(text(
                                "SELECT count(*) FROM pg_stat_activity "
                                "WHERE application_name = :app AND wait_event_type = 'Lock'"
                            ), {"app": app_name})
                        if count == 2:
                            return
                        await asyncio.sleep(0.01)

                await asyncio.wait_for(both_waiting(), timeout=10)
            results = await asyncio.wait_for(asyncio.gather(*requests), timeout=10)
        finally:
            for request in requests:
                if not request.done():
                    request.cancel()
            await asyncio.gather(*requests, return_exceptions=True)
        assert sorted(response.status_code for response in results) == [200, 400]
        rotated = next(response.json() for response in results if response.status_code == 200)
        assert await _mcp_status(client, rotated["access_token"]) == 200
        assert await _mcp_status(client, tokens["access_token"]) == 401
