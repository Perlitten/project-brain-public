"""OIDC single sign-on for team members against a fake identity provider.

The provider is an ``httpx.MockTransport`` with a locally generated RSA key: it
serves discovery and JWKS, checks PKCE and client authentication at the token
endpoint and returns whatever ID token claims a test asks for. No network.
"""
import base64
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import ValidationError
from sqlalchemy import select, update

from apps.api.invitation_throttle import InProcessWindow
from apps.api.routers import team_invitations, team_oidc, web_session
from brain.config.settings import Settings, settings
from brain.database.models import ApiCredential
from brain.team import invitations, oidc
from brain.team.authorization import TeamConflict
from brain.team.models import TeamIdentity, TeamInvitation, TeamMember, TeamOidcFlow
from tests.test_team_foundations import team_db as team_fixture
from tests.test_team_transport import client_for, membership_driven, personal_app

team_db = team_fixture
pytestmark = pytest.mark.team_state

ISSUER = "https://idp.example.test"
CLIENT_ID = "brain-dashboard"
CLIENT_SECRET = "fixture-client-secret"
REDIRECT = "https://web.example.test/auth/oidc/callback"
BINDING = "b" * 43
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeIdP:
    """Discovery, JWKS and a token endpoint that redeems codes registered by the test."""

    def __init__(self):
        self.codes: dict[str, dict] = {}
        self.discovery_issuer = ISSUER
        self.requests: list[str] = []
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key()))
        self.jwks = {"keys": [{**jwk, "kid": "k1", "use": "sig", "alg": "RS256"}]}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(f"{request.method} {request.url.path}")
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(200, json={
                "issuer": self.discovery_issuer,
                "authorization_endpoint": f"{ISSUER}/authorize",
                "token_endpoint": f"{ISSUER}/token",
                "jwks_uri": f"{ISSUER}/jwks",
                "id_token_signing_alg_values_supported": ["RS256"],
                "token_endpoint_auth_methods_supported": ["client_secret_basic"],
            })
        if request.url.path == "/jwks":
            return httpx.Response(200, json=self.jwks)
        if request.url.path == "/token" and request.method == "POST":
            form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
            expected = "Basic " + base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
            if request.headers.get("authorization") != expected:
                return httpx.Response(401, json={"error": "invalid_client"})
            grant = self.codes.pop(form.get("code", ""), None)
            if grant is None or form.get("redirect_uri") != REDIRECT or form.get("grant_type") != "authorization_code":
                return httpx.Response(400, json={"error": "invalid_grant"})
            challenge = base64.urlsafe_b64encode(
                hashlib.sha256(form.get("code_verifier", "").encode()).digest()).rstrip(b"=").decode()
            if challenge != grant["challenge"]:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "PKCE"})
            return httpx.Response(200, json={"access_token": "opaque", "token_type": "Bearer",
                                             "id_token": grant["id_token"]})
        return httpx.Response(404)


def id_token(nonce, *, key=KEY, algorithm="RS256", kid="k1", **claims):
    now = int(time.time())
    body = {"iss": ISSUER, "aud": CLIENT_ID, "sub": "alice-sub", "email": "alice@example.test",
            "email_verified": True, "iat": now, "exp": now + 300, "nonce": nonce, **claims}
    body = {name: value for name, value in body.items() if value is not None}
    return jwt.encode(body, key, algorithm=algorithm, headers={"kid": kid})


@pytest.fixture
def idp(monkeypatch):
    provider = FakeIdP()
    monkeypatch.setattr(oidc, "http_transport", httpx.MockTransport(provider.handler))
    monkeypatch.setattr(settings, "BRAIN_OIDC_ENABLED", True)
    monkeypatch.setattr(settings, "BRAIN_OIDC_ISSUER", ISSUER)
    monkeypatch.setattr(settings, "BRAIN_OIDC_CLIENT_ID", CLIENT_ID)
    monkeypatch.setattr(settings, "BRAIN_OIDC_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.setattr(settings, "BRAIN_OIDC_REDIRECT_URI", REDIRECT)
    monkeypatch.setattr(settings, "BRAIN_OIDC_SCOPES", "openid email profile")
    monkeypatch.setattr(settings, "BRAIN_OIDC_ALLOWED_EMAIL_DOMAINS", "")
    monkeypatch.setattr(settings, "BRAIN_OIDC_REQUIRE_FOR_INVITATIONS", False)
    oidc.reset_caches()
    monkeypatch.setattr(team_oidc, "_throttle", InProcessWindow())
    monkeypatch.setattr(team_invitations, "_throttle", InProcessWindow())
    yield provider
    oidc.reset_caches()


def sso_app(factory, monkeypatch):
    membership_driven(monkeypatch)
    app = personal_app(factory, monkeypatch)
    for module in (team_oidc, team_invitations, web_session):
        monkeypatch.setattr(module, "async_session_factory", factory)
    app.include_router(team_oidc.router)
    app.include_router(team_invitations.router)
    app.include_router(web_session.router)
    return app


async def start(client, invitation=None, binding=BINDING):
    body = {"binding": binding, **({"invitation": invitation} if invitation else {})}
    response = await client.post("/team/oidc/start", json=body)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    query = {key: values[0] for key, values in parse_qs(urlsplit(payload["authorization_url"]).query).items()}
    return payload, query


async def sign_in_flow(client, idp, *, code="code-1", binding=BINDING, invitation=None, token=None, **claims):
    """Start, let the fake provider issue a code for ``claims``, and finish at the callback."""
    payload, query = await start(client, invitation=invitation)
    idp.codes[code] = {"challenge": query["code_challenge"],
                       "id_token": token(query["nonce"]) if token else id_token(query["nonce"], **claims)}
    return await client.post("/team/oidc/callback", json={"state": payload["state"], "code": code, "binding": binding})


async def bind(factory, principal="human-1", subject="alice-sub"):
    async with factory() as session, session.begin():
        return await oidc.bind_identity(session, principal_name=principal, subject=subject,
                                        email="alice@example.test", bound_by="test")


async def invite(factory, email="Carol@Example.test", name="carol", project=None):
    async with factory() as session, session.begin():
        return await invitations.create_invitation(
            session, invited_by="principal:4", invited_by_principal_id=4, role="reader", email=email, name=name,
            projects=[str(project)] if project else None)


# --- sign-in ------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sign_in_happy_path_issues_a_member_session(team_db, monkeypatch, idp):
    factory, _, _ = team_db
    await bind(factory)
    async with client_for(sso_app(factory, monkeypatch)) as client:
        assert (await client.get("/team/oidc/config")).json() == {"enabled": True, "require_for_invitations": False}
        payload, query = await start(client)
        assert payload["purpose"] == "signin"
        assert payload["authorization_url"].startswith(f"{ISSUER}/authorize?")
        assert query["response_type"] == "code" and query["client_id"] == CLIENT_ID
        assert query["redirect_uri"] == REDIRECT and query["scope"] == "openid email profile"
        assert query["code_challenge_method"] == "S256" and query["state"] == payload["state"]
        assert len(query["nonce"]) >= 32 and "login_hint" not in query
        idp.codes["code-1"] = {"challenge": query["code_challenge"], "id_token": id_token(query["nonce"])}
        done = await client.post("/team/oidc/callback",
                                 json={"state": payload["state"], "code": "code-1", "binding": BINDING})
        assert done.status_code == 200, done.text
        assert done.headers["cache-control"] == "no-store"
        result = done.json()
        assert result["mode"] == "signin" and result["member"]["principal_name"] == "human-1"
        credential = result["credential"]
        session = await client.get("/api/web-session", headers={"X-API-Key": credential})
        assert session.status_code == 200, session.text
        assert session.json()["principal"]["name"] == "human-1" and session.json()["team_admin"] is False
    async with factory() as db:
        rows = (await db.execute(select(ApiCredential).where(ApiCredential.principal_id == 1))).scalars().all()
        minted = [row for row in rows if row.id != 1]
        identity = (await db.execute(select(TeamIdentity))).scalar_one()
    assert len(minted) == 1 and "*" not in minted[0].scopes
    expires = minted[0].expires_at if minted[0].expires_at.tzinfo else minted[0].expires_at.replace(tzinfo=timezone.utc)
    assert timedelta(days=6) < expires - datetime.now(timezone.utc) <= timedelta(days=7)
    assert identity.last_login_at is not None
    assert credential not in json.dumps(idp.requests)


@pytest.mark.asyncio
async def test_admin_sign_in_gets_admin_scopes(team_db, monkeypatch, idp):
    factory, _, _ = team_db
    await bind(factory, principal="human-4", subject="admin-sub")
    async with client_for(sso_app(factory, monkeypatch)) as client:
        done = await sign_in_flow(client, idp, sub="admin-sub", email="root@example.test")
        assert done.status_code == 200, done.text
        session = await client.get("/api/web-session", headers={"X-API-Key": done.json()["credential"]})
        assert session.json()["team_admin"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("claims, message", [
    ({"aud": "someone-else"}, oidc.TOKEN_REJECTED),
    ({"exp": int(time.time()) - 3600, "iat": int(time.time()) - 7200}, oidc.TOKEN_REJECTED),
    ({"iss": "https://evil.example.test"}, oidc.TOKEN_REJECTED),
    ({"aud": [CLIENT_ID, "other"]}, oidc.TOKEN_REJECTED),  # several audiences and no azp
    ({"email_verified": False}, "has not verified this account's email"),
    ({"email_verified": None}, "has not verified this account's email"),
    ({"email": None}, "did not return an email address"),
])
async def test_sign_in_refuses_invalid_id_tokens(team_db, monkeypatch, idp, claims, message):
    factory, _, _ = team_db
    await bind(factory)
    async with client_for(sso_app(factory, monkeypatch)) as client:
        done = await sign_in_flow(client, idp, **claims)
    assert done.status_code == 403, done.text
    assert message in done.json()["detail"]
    async with factory() as db:
        assert (await db.execute(select(ApiCredential).where(ApiCredential.principal_id == 1))).scalars().all()[0].id == 1
        assert len((await db.execute(select(ApiCredential))).scalars().all()) == 4


@pytest.mark.asyncio
async def test_sign_in_refuses_nonce_mismatch(team_db, monkeypatch, idp):
    factory, _, _ = team_db
    await bind(factory)
    async with client_for(sso_app(factory, monkeypatch)) as client:
        done = await sign_in_flow(client, idp, token=lambda _nonce: id_token("an-older-nonce"))
        missing = await sign_in_flow(client, idp, code="code-2", token=lambda _nonce: id_token(None))
    assert done.status_code == 403 and done.json()["detail"] == oidc.TOKEN_REJECTED
    assert missing.status_code == 403 and missing.json()["detail"] == oidc.TOKEN_REJECTED


@pytest.mark.asyncio
async def test_sign_in_refuses_forged_signatures_and_symmetric_algorithms(team_db, monkeypatch, idp):
    factory, _, _ = team_db
    await bind(factory)
    async with client_for(sso_app(factory, monkeypatch)) as client:
        forged = await sign_in_flow(client, idp, token=lambda nonce: id_token(nonce, key=OTHER_KEY))
        unknown_kid = await sign_in_flow(client, idp, code="c2", token=lambda nonce: id_token(nonce, kid="k9"))
        hmac_token = await sign_in_flow(client, idp, code="c3", token=lambda nonce: id_token(
            nonce, key=CLIENT_SECRET.encode() * 2, algorithm="HS256"))
        unsigned = await sign_in_flow(client, idp, code="c4", token=lambda nonce: jwt.encode(
            {"iss": ISSUER, "aud": CLIENT_ID, "sub": "alice-sub", "nonce": nonce}, None, algorithm="none"))
    for response in (forged, unknown_kid, hmac_token, unsigned):
        assert response.status_code == 403 and response.json()["detail"] == oidc.TOKEN_REJECTED


@pytest.mark.asyncio
async def test_unknown_subject_is_refused_without_provisioning(team_db, monkeypatch, idp):
    factory, _, _ = team_db
    async with client_for(sso_app(factory, monkeypatch)) as client:
        done = await sign_in_flow(client, idp, sub="stranger", email="stranger@example.test")
    assert done.status_code == 403 and done.json()["detail"] == oidc.NOT_LINKED
    async with factory() as db:
        assert len((await db.execute(select(TeamMember))).scalars().all()) == 4
        assert (await db.execute(select(TeamIdentity))).first() is None


@pytest.mark.asyncio
async def test_revoked_member_is_refused(team_db, monkeypatch, idp):
    factory, _, _ = team_db
    await bind(factory)
    async with factory() as session, session.begin():
        await session.execute(update(TeamMember).where(TeamMember.principal_id == 1)
                              .values(revoked_at=datetime.now(timezone.utc)))
    async with client_for(sso_app(factory, monkeypatch)) as client:
        done = await sign_in_flow(client, idp)
    assert done.status_code == 403 and done.json()["detail"] == oidc.NOT_LINKED


@pytest.mark.asyncio
async def test_flow_is_single_use_and_bound_to_the_browser(team_db, monkeypatch, idp):
    factory, _, _ = team_db
    await bind(factory)
    async with client_for(sso_app(factory, monkeypatch)) as client:
        stolen = await sign_in_flow(client, idp, binding="x" * 43)
        assert stolen.status_code == 403 and stolen.json()["detail"] == oidc.FLOW_REJECTED
        payload, query = await start(client)
        idp.codes["code-9"] = {"challenge": query["code_challenge"], "id_token": id_token(query["nonce"])}
        body = {"state": payload["state"], "code": "code-9", "binding": BINDING}
        first = await client.post("/team/oidc/callback", json=body)
        replay = await client.post("/team/oidc/callback", json=body)
        bogus = await client.post("/team/oidc/callback", json={**body, "state": "s" * 43})
    assert first.status_code == 200, first.text
    assert replay.status_code == 403 and replay.json()["detail"] == oidc.FLOW_REJECTED
    assert bogus.status_code == 403 and bogus.json()["detail"] == oidc.FLOW_REJECTED


@pytest.mark.asyncio
async def test_expired_flow_and_refused_code_are_rejected(team_db, monkeypatch, idp):
    factory, _, _ = team_db
    await bind(factory)
    async with client_for(sso_app(factory, monkeypatch)) as client:
        payload, query = await start(client)
        async with factory() as session, session.begin():
            await session.execute(update(TeamOidcFlow).values(
                expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
        idp.codes["late"] = {"challenge": query["code_challenge"], "id_token": id_token(query["nonce"])}
        late = await client.post("/team/oidc/callback", json={"state": payload["state"], "code": "late",
                                                              "binding": BINDING})
        payload, _ = await start(client)
        refused = await client.post("/team/oidc/callback", json={"state": payload["state"], "code": "never-issued",
                                                                 "binding": BINDING})
    assert late.status_code == 403 and late.json()["detail"] == oidc.FLOW_REJECTED
    assert refused.status_code == 403 and "refused the authorization code" in refused.json()["detail"]


@pytest.mark.asyncio
async def test_allowed_domains_and_issuer_mismatch(team_db, monkeypatch, idp):
    factory, _, _ = team_db
    await bind(factory)
    monkeypatch.setattr(settings, "BRAIN_OIDC_ALLOWED_EMAIL_DOMAINS", "corp.example.test, @Example.Test")
    async with client_for(sso_app(factory, monkeypatch)) as client:
        allowed = await sign_in_flow(client, idp, email="Alice@EXAMPLE.test")
        outside = await sign_in_flow(client, idp, code="c2", email="alice@gmail.example")
        assert allowed.status_code == 200, allowed.text
        assert outside.status_code == 403 and "email domain" in outside.json()["detail"]
        oidc.reset_caches()
        idp.discovery_issuer = "https://other.example.test"
        unavailable = await client.post("/team/oidc/start", json={"binding": BINDING})
    assert unavailable.status_code == 502 and "different issuer" in unavailable.json()["detail"]


@pytest.mark.asyncio
async def test_oidc_disabled_answers_404_everywhere(team_db, monkeypatch, idp):
    factory, _, _ = team_db
    monkeypatch.setattr(settings, "BRAIN_OIDC_ENABLED", False)
    async with client_for(sso_app(factory, monkeypatch)) as client:
        config = await client.get("/team/oidc/config")
        begin = await client.post("/team/oidc/start", json={"binding": BINDING})
        finish = await client.post("/team/oidc/callback",
                                   json={"state": "s" * 43, "code": "c", "binding": BINDING})
    assert config.status_code == 200 and config.json() == {"enabled": False, "require_for_invitations": False}
    assert begin.status_code == 404 and begin.json()["detail"] == oidc.DISABLED
    assert finish.status_code == 404
    assert idp.requests == []


@pytest.mark.asyncio
async def test_start_is_throttled_per_address(team_db, monkeypatch, idp):
    factory, _, _ = team_db
    monkeypatch.setattr(team_oidc, "OIDC_LIMIT", 2)
    async with client_for(sso_app(factory, monkeypatch)) as client:
        answers = [await client.post("/team/oidc/start", json={"binding": BINDING}) for _ in range(3)]
    assert [answer.status_code for answer in answers] == [200, 200, 429]
    assert int(answers[-1].headers["Retry-After"]) >= 1


# --- invitations --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invitation_accepted_through_sso_then_member_signs_in(team_db, monkeypatch, idp):
    factory, a, _ = team_db
    token, view = await invite(factory, project=a)
    async with client_for(sso_app(factory, monkeypatch)) as client:
        payload, query = await start(client, invitation=token)
        assert payload["purpose"] == "invitation" and query["login_hint"] == "Carol@Example.test"
        idp.codes["inv"] = {"challenge": query["code_challenge"],
                            "id_token": id_token(query["nonce"], sub="carol-sub", email="carol@example.TEST")}
        done = await client.post("/team/oidc/callback",
                                 json={"state": payload["state"], "code": "inv", "binding": BINDING})
        assert done.status_code == 200, done.text
        result = done.json()
        assert result["mode"] == "invitation" and result["credential"]
        assert result["invitation"]["verified_via"] == "oidc"
        assert result["invitation"]["verified_issuer"] == ISSUER
        assert result["invitation"]["verified_subject"] == "carol-sub"
        assert result["member"]["principal_name"] == "carol"
        assert [row["project_id"] for row in result["project_memberships"]] == [str(a)]
        session = await client.get("/api/web-session", headers={"X-API-Key": result["credential"]})
        assert session.status_code == 200 and session.json()["principal"]["name"] == "carol"
        # The token is spent: neither a token redemption nor a second SSO acceptance can reuse it.
        retry_token = await client.post("/team/invitations/accept", json={"token": token})
        assert retry_token.status_code == 200 and retry_token.json()["credential"] is None
        again = await client.post("/team/oidc/start", json={"binding": BINDING, "invitation": token})
        assert again.status_code == 403
        later = await sign_in_flow(client, idp, code="later", sub="carol-sub", email="carol@example.test")
        assert later.status_code == 200 and later.json()["mode"] == "signin"
    async with factory() as db:
        member = (await db.execute(select(TeamMember).where(TeamMember.principal_id == result["member"]["principal_id"]))
                  ).scalar_one()
        identity = (await db.execute(select(TeamIdentity))).scalar_one()
        invitation = (await db.execute(select(TeamInvitation))).scalar_one()
    assert (member.verification_issuer, member.verification_subject) == (ISSUER, "carol-sub")
    assert identity.bound_by == f"invitation:{view['id']}" and str(identity.invitation_id) == view["id"]
    assert (invitation.verified_issuer, invitation.verified_subject) == (ISSUER, "carol-sub")


@pytest.mark.asyncio
async def test_invitation_email_mismatch_and_unverified_email_are_refused(team_db, monkeypatch, idp):
    factory, _, _ = team_db
    token, _ = await invite(factory)
    async with client_for(sso_app(factory, monkeypatch)) as client:
        mismatch = await sign_in_flow(client, idp, invitation=token, sub="mallory", email="mallory@example.test")
        lookalike = await sign_in_flow(client, idp, code="c2", invitation=token, sub="mallory",
                                       email="carol@example.test.evil")
        unverified = await sign_in_flow(client, idp, code="c3", invitation=token, sub="carol-sub",
                                        email="carol@example.test", email_verified=False)
    assert mismatch.status_code == 403 and mismatch.json()["detail"] == invitations.EMAIL_MISMATCH
    assert lookalike.status_code == 403 and lookalike.json()["detail"] == invitations.EMAIL_MISMATCH
    assert unverified.status_code == 403 and "has not verified" in unverified.json()["detail"]
    async with factory() as db:
        invitation = (await db.execute(select(TeamInvitation))).scalar_one()
    assert invitation.accepted_at is None and invitation.verified_subject is None


@pytest.mark.asyncio
async def test_invitation_without_email_cannot_use_sso(team_db, monkeypatch, idp):
    factory, _, _ = team_db
    token, _ = await invite(factory, email=None)
    async with client_for(sso_app(factory, monkeypatch)) as client:
        response = await client.post("/team/oidc/start", json={"binding": BINDING, "invitation": token})
        bogus = await client.post("/team/oidc/start", json={"binding": BINDING, "invitation": "pbi_nope"})
    assert response.status_code == 409 and "names no email address" in response.json()["detail"]
    assert bogus.status_code == 403 and bogus.json()["detail"] == invitations.REJECTED


@pytest.mark.asyncio
async def test_sso_account_already_linked_cannot_accept_another_invitation(team_db, monkeypatch, idp):
    factory, _, _ = team_db
    await bind(factory, subject="carol-sub")
    token, _ = await invite(factory)
    async with client_for(sso_app(factory, monkeypatch)) as client:
        done = await sign_in_flow(client, idp, invitation=token, sub="carol-sub", email="carol@example.test")
    assert done.status_code == 409 and "already linked" in done.json()["detail"]


@pytest.mark.asyncio
async def test_require_sso_for_invitations_refuses_token_acceptance(team_db, monkeypatch, idp):
    factory, _, _ = team_db
    token, _ = await invite(factory)
    monkeypatch.setattr(settings, "BRAIN_OIDC_REQUIRE_FOR_INVITATIONS", True)
    async with client_for(sso_app(factory, monkeypatch)) as client:
        assert (await client.get("/team/oidc/config")).json()["require_for_invitations"] is True
        by_token = await client.post("/team/invitations/accept", json={"token": token})
        no_email = await client.post("/team/invitations", headers={"X-API-Key": "fixture-4"},
                                     json={"role": "reader", "name": "dave"})
        by_sso = await sign_in_flow(client, idp, invitation=token, sub="carol-sub", email="carol@example.test")
    assert by_token.status_code == 409 and "/auth/oidc/start?invitation=" in by_token.json()["detail"]
    assert no_email.status_code == 409 and "email address" in no_email.json()["detail"]
    assert by_sso.status_code == 200 and by_sso.json()["mode"] == "invitation"


@pytest.mark.asyncio
async def test_token_acceptance_still_works_when_sso_is_off(team_db, monkeypatch, idp):
    factory, _, _ = team_db
    monkeypatch.setattr(settings, "BRAIN_OIDC_ENABLED", False)
    monkeypatch.setattr(settings, "BRAIN_OIDC_REQUIRE_FOR_INVITATIONS", True)  # inert while SSO is off
    token, _ = await invite(factory, email=None)
    async with client_for(sso_app(factory, monkeypatch)) as client:
        accepted = await client.post("/team/invitations/accept", json={"token": token})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["invitation"]["verified_via"] == "token"


# --- operator bindings and configuration --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_operator_binding_rules(team_db, monkeypatch, idp):
    factory, _, _ = team_db
    first = await bind(factory)
    assert first["issuer"] == ISSUER and first["principal_name"] == "human-1"
    assert (await bind(factory))["id"] == first["id"]  # idempotent
    with pytest.raises(TeamConflict, match="already bound to another member"):
        await bind(factory, principal="human-2")
    with pytest.raises(TeamConflict, match="unbind it first"):
        await bind(factory, subject="second-sub")
    async with factory() as session, session.begin():
        await session.execute(update(TeamMember).where(TeamMember.principal_id == 3)
                              .values(revoked_at=datetime.now(timezone.utc)))
    with pytest.raises(TeamConflict, match="not an active team member"):
        await bind(factory, principal="human-3", subject="third-sub")
    async with factory() as session, session.begin():
        assert await oidc.unbind_identity(session, principal_name="human-1") == 1
    async with client_for(sso_app(factory, monkeypatch)) as client:
        done = await sign_in_flow(client, idp)
    assert done.status_code == 403 and done.json()["detail"] == oidc.NOT_LINKED


@pytest.mark.parametrize("env, message", [
    ({}, "BRAIN_OIDC_ISSUER"),
    ({"BRAIN_OIDC_ISSUER": ISSUER}, "BRAIN_OIDC_CLIENT_ID"),
    ({"BRAIN_OIDC_ISSUER": "http://idp.example.test", "BRAIN_OIDC_CLIENT_ID": "x",
      "BRAIN_OIDC_REDIRECT_URI": REDIRECT}, "https"),
    ({"BRAIN_OIDC_ISSUER": ISSUER, "BRAIN_OIDC_CLIENT_ID": "x", "BRAIN_OIDC_REDIRECT_URI": REDIRECT,
      "BRAIN_OIDC_SCOPES": "email"}, "openid"),
])
def test_settings_validate_enabled_oidc(env, message):
    with pytest.raises(ValidationError, match=message):
        Settings(_env_file=None, BRAIN_OIDC_ENABLED=True, **env)


def test_settings_default_redirect_and_loopback_issuer():
    configured = Settings(_env_file=None, BRAIN_OIDC_ENABLED=True, BRAIN_OIDC_ISSUER="http://127.0.0.1:8080/",
                          BRAIN_OIDC_CLIENT_ID="x", BRAIN_WEB_URL="https://web.example.test/")
    assert configured.BRAIN_OIDC_ISSUER == "http://127.0.0.1:8080"
    assert configured.BRAIN_OIDC_REDIRECT_URI == REDIRECT
    assert Settings(_env_file=None).BRAIN_OIDC_ENABLED is False


def test_cli_sso_bind_list_unbind(team_db, monkeypatch, idp):
    from typer.testing import CliRunner

    from apps.cli.commands import team as team_commands
    from apps.cli.main import cli

    factory, _, _ = team_db
    monkeypatch.setattr(team_commands, "async_session_factory", factory)
    runner = CliRunner()

    def invoke(*args):
        return runner.invoke(cli, ["team", "member", *args])

    bound = invoke("sso-bind", "--principal", "human-2", "--subject", "bob-sub", "--email", "bob@example.test")
    assert bound.exit_code == 0, bound.output
    assert json.loads(bound.output)["bound_by"] == "operator:cli" and json.loads(bound.output)["issuer"] == ISSUER
    taken = invoke("sso-bind", "--principal", "3", "--subject", "bob-sub")
    assert taken.exit_code == 1 and "already bound to another member" in taken.output
    listing = json.loads(invoke("sso-list").output)
    assert [(row["principal_name"], row["subject"]) for row in listing] == [("human-2", "bob-sub")]
    removed = invoke("sso-unbind", "--principal", "human-2")
    assert removed.exit_code == 0 and json.loads(removed.output) == {"principal": "human-2", "removed": 1}
    assert json.loads(invoke("sso-list").output) == []


def test_sso_throttle_is_shared_under_full_storage(monkeypatch):
    monkeypatch.setattr(team_oidc, "_throttle", None)
    monkeypatch.setattr(settings, "BRAIN_STORAGE_MODE", "full")
    shared = team_oidc.oidc_throttle()
    assert isinstance(shared, team_oidc.RedisWindow)
    assert shared._prefix == team_oidc.OIDC_KEY_PREFIX != "brain:team:invitation-accept:"
    monkeypatch.setattr(team_oidc, "_throttle", None)
    monkeypatch.setattr(settings, "BRAIN_STORAGE_MODE", "lite")
    assert isinstance(team_oidc.oidc_throttle(), InProcessWindow)
