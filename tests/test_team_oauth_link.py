"""OAuth clients as team members: ``team:*`` scopes bind an issued token to the
verified member who approved it, within that member's live project roles.

Runs on the sqlite team fixture; the Postgres end-to-end OAuth flow stays in
``test_mcp_oauth_db``.
"""
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from mcp.server.auth.provider import AuthorizationCode, AuthorizationParams, RefreshToken, TokenError
from mcp.shared.auth import OAuthClientInformationFull
from sqlalchemy import select, update

from apps.api import auth, mcp_oauth
from brain.auth import oauth_store
from brain.auth.principals import resolve_credential
from brain.config.settings import settings
from brain.database.models import ApiCredential, OAuthRefreshToken, Principal
from brain.team import oauth_link
from brain.team.authorization import TeamDenied
from brain.team.models import ProjectMember, TeamMember
from brain.team.provisioning import CREDENTIAL_ISSUER, WEB_LOGIN_ISSUER
from tests.test_team_foundations import team_db as team_fixture

team_db = team_fixture
TTL = timedelta(hours=1)
CLIENT_ID = "client-1"


async def verify_by_credential(factory, principal_id, issuer=CREDENTIAL_ISSUER, subject=None):
    """Re-verify a fixture member the way ``brain team member add`` records it."""
    async with factory() as session, session.begin():
        await session.execute(update(TeamMember).where(TeamMember.principal_id == principal_id).values(
            verification_issuer=issuer, verification_subject=subject or f"human-{principal_id}"))


async def revoke_roles(factory, principal_id):
    async with factory() as session, session.begin():
        await session.execute(update(ProjectMember).where(ProjectMember.principal_id == principal_id)
                              .values(revoked_at=datetime.now(timezone.utc)))


async def register_client(factory):
    async with factory() as session, session.begin():
        await oauth_store.save_client(session, CLIENT_ID, {"client_id": CLIENT_ID, "client_name": "Reader"})


async def grant(factory, issuer, subject):
    async with factory() as session:
        return await oauth_link.member_grant(session, issuer, subject)


async def issue(factory, subject, scopes):
    async with factory() as session, session.begin():
        return await oauth_store.issue_tokens(session, client_id=CLIENT_ID, client_name="Reader", scopes=scopes,
                                              subject=subject, access_ttl=TTL, refresh_ttl=TTL)


async def rotate(factory, refresh_token, scopes):
    async with factory() as session, session.begin():
        return await oauth_store.rotate_refresh(session, refresh_token, client_name="Reader", scopes=scopes,
                                                access_ttl=TTL, refresh_ttl=TTL)


async def owner_of(factory, access_token):
    async with factory() as session:
        return await resolve_credential(session, access_token)


def test_approver_identity_and_team_scope_catalogue():
    assert oauth_link.approver_identity(None) is None
    assert oauth_link.approver_identity(oauth_link.DEV_OPEN_SUBJECT) is None
    assert oauth_link.approver_identity("web:") is None
    assert oauth_link.approver_identity("web:alice") == (WEB_LOGIN_ISSUER, "alice")
    assert oauth_link.approver_identity("human-2") == (CREDENTIAL_ISSUER, "human-2")
    assert oauth_link.team_scopes(["core:read", "team:read", "harness:write"]) == ["team:read"]
    assert {"team:read", "team:admin", "team:task.create", "team:knowledge.approve"} <= set(oauth_link.TEAM_SCOPES)
    # Execution authority is M2: no role grants it, so OAuth cannot issue it.
    assert not {"team:execute", "team:merge", "team:provider.transfer"} & set(oauth_link.TEAM_SCOPES)
    assert set(oauth_link.TEAM_SCOPES) <= set(mcp_oauth.SUPPORTED_SCOPES)
    assert not oauth_link.team_scopes(mcp_oauth.DEFAULT_SCOPES)


@pytest.mark.asyncio
async def test_member_grant_follows_live_roles_and_membership(team_db):
    factory, a, _ = team_db
    assert (await grant(factory, "fixture", "1")).scopes == {"team:read"}
    reviewer = await grant(factory, "fixture", "3")
    assert reviewer.principal.id == 3 and {"team:knowledge.approve", "team:task.accept"} <= reviewer.scopes
    administrator = await grant(factory, "fixture", "4")
    assert {"team:admin", "team:task.create"} <= administrator.scopes and "team:knowledge.approve" not in administrator.scopes
    assert await grant(factory, "fixture", "9") is None
    assert await grant(factory, "other-issuer", "1") is None

    await revoke_roles(factory, 2)
    assert (await grant(factory, "fixture", "2")).scopes == frozenset()
    async with factory() as session, session.begin():
        await session.execute(update(TeamMember).where(TeamMember.principal_id == 1).values(revoked_at=datetime.now(timezone.utc)))
        await session.execute(update(TeamMember).where(TeamMember.principal_id == 3).values(verified_at=None))
        await session.execute(update(Principal).where(Principal.id == 4).values(kind="service"))
    for subject in ("1", "3", "4"):
        assert await grant(factory, "fixture", subject) is None


@pytest.mark.asyncio
async def test_bind_team_scopes_requires_member_within_roles(team_db):
    factory, _, _ = team_db
    async with factory() as session:
        assert await oauth_link.bind_team_scopes(session, oauth_link.DEV_OPEN_SUBJECT, ["core:read"]) is None
        assert await oauth_link.bind_team_scopes(session, None, ["core:write", "jobs:read"]) is None
        for subject in (oauth_link.DEV_OPEN_SUBJECT, None, "human-1", "web:alice", "nobody"):
            with pytest.raises(TeamDenied, match="verified team member"):
                await oauth_link.bind_team_scopes(session, subject, ["team:read"])
    await verify_by_credential(factory, 1)
    await verify_by_credential(factory, 2, issuer=WEB_LOGIN_ISSUER, subject="alice")
    async with factory() as session:
        bound = await oauth_link.bind_team_scopes(session, "human-1", ["core:read", "team:read"])
        assert bound.principal.id == 1 and bound.member.principal_id == 1
        with pytest.raises(TeamDenied, match="outside the member's roles: team:task.create"):
            await oauth_link.bind_team_scopes(session, "human-1", ["team:read", "team:task.create"])
        assert (await oauth_link.bind_team_scopes(session, "web:alice", ["team:task.create"])).principal.id == 2


@pytest.mark.asyncio
async def test_issue_tokens_mints_on_member_and_refresh_reevaluates(team_db):
    factory, _, _ = team_db
    await register_client(factory)
    await verify_by_credential(factory, 2)

    personal = await issue(factory, "human-2", ["core:read"])
    owner = await owner_of(factory, personal.access_token)
    assert owner.name.startswith("oauth:Reader:") and owner.kind == "service" and owner.scopes == {"core:read"}

    team = await issue(factory, "human-2", ["core:read", "team:read", "team:task.create"])
    owner = await owner_of(factory, team.access_token)
    assert owner.id == 2 and owner.kind == "human" and owner.scopes == {"core:read", "team:read", "team:task.create"}
    async with factory() as session:
        rows = (await session.execute(select(OAuthRefreshToken).order_by(OAuthRefreshToken.id))).scalars().all()
        assert [row.principal_name for row in rows] == [oauth_store.principal_name_for(CLIENT_ID, "Reader"), "human-2"]
        credential = await session.get(ApiCredential, rows[1].access_credential_id)
        assert credential.principal_id == 2 and credential.expires_at is not None

    with pytest.raises(TeamDenied, match="outside the member's roles"):
        await issue(factory, "human-2", ["team:knowledge.approve"])
    with pytest.raises(TeamDenied, match="verified team member"):
        await issue(factory, "human-9", ["team:read"])

    rotated = await rotate(factory, team.refresh_token, ["core:read", "team:read"])
    assert (await owner_of(factory, rotated.access_token)).id == 2
    assert await owner_of(factory, team.access_token) is None

    await revoke_roles(factory, 2)
    with pytest.raises(TeamDenied, match="team:read"):
        await rotate(factory, rotated.refresh_token, ["core:read", "team:read"])
    # The denial rolled back: the client may still refresh without team scopes.
    narrowed = await rotate(factory, rotated.refresh_token, ["core:read"])
    assert (await owner_of(factory, narrowed.access_token)).name.startswith("oauth:Reader:")
    assert (await rotate(factory, personal.refresh_token, ["core:read"])) is not None


def _code(subject, scopes):
    return AuthorizationCode(code="code-1", scopes=scopes, expires_at=time.time() + 60, client_id=CLIENT_ID,
                             code_challenge="challenge", redirect_uri="http://127.0.0.1:9999/callback",
                             redirect_uri_provided_explicitly=True, subject=subject)


@pytest.mark.asyncio
async def test_provider_reports_team_denials_as_invalid_scope(team_db, monkeypatch):
    factory, _, _ = team_db
    monkeypatch.setattr(mcp_oauth, "async_session_factory", factory)
    monkeypatch.setattr(auth, "async_session_factory", factory)
    await register_client(factory)
    await verify_by_credential(factory, 1)
    provider = mcp_oauth.BrainOAuthProvider("http://localhost")
    client = OAuthClientInformationFull(client_id=CLIENT_ID, client_name="Reader",
                                        redirect_uris=["http://127.0.0.1:9999/callback"])

    code = _code("human-9", ["team:read"])
    provider._codes[code.code] = code
    with pytest.raises(TokenError) as denied:
        await provider.exchange_authorization_code(client, code)
    assert denied.value.error == "invalid_scope" and "verified team member" in denied.value.error_description

    code = _code("human-1", ["core:read", "team:read"])
    provider._codes[code.code] = code
    token = await provider.exchange_authorization_code(client, code)
    assert set(token.scope.split()) == {"core:read", "team:read"}
    principal = await auth.resolve_api_key(token.access_token)
    assert principal.id == 1 and principal.has_scope("team:read")

    refresh = RefreshToken(token=token.refresh_token, client_id=CLIENT_ID, scopes=["core:read", "team:read"],
                           subject="human-1")
    renewed = await provider.exchange_refresh_token(client, refresh, [])
    assert (await auth.resolve_api_key(renewed.access_token)).id == 1
    await revoke_roles(factory, 1)
    refresh = RefreshToken(token=renewed.refresh_token, client_id=CLIENT_ID, scopes=["core:read", "team:read"])
    with pytest.raises(TokenError) as denied:
        await provider.exchange_refresh_token(client, refresh, [])
    assert denied.value.error == "invalid_scope" and "team:read" in denied.value.error_description


@pytest.mark.asyncio
async def test_consent_approver_explains_team_denials(team_db, monkeypatch):
    factory, _, _ = team_db
    monkeypatch.setattr(mcp_oauth, "async_session_factory", factory)
    monkeypatch.setattr(auth, "async_session_factory", factory)
    approve = mcp_oauth._approver
    assert await approve("", "", "fixture-1", ["core:read"], "10.0.0.1") == ("human-1", "")
    assert await approve("", "", "wrong-key", ["core:read"], "10.0.0.2") == ("", "That API key is not valid.")
    assert await approve("", "", "fixture-1", ["core:read", "team:read"], "10.0.0.3") == (
        "", "Team scopes require approval by a verified team member.")
    await verify_by_credential(factory, 1)
    assert await approve("", "", "fixture-1", ["core:read", "team:read"], "10.0.0.4") == ("human-1", "")
    subject, error = await approve("", "", "fixture-1", ["team:read", "team:task.create"], "10.0.0.5")
    assert subject == "" and error == "Team scopes outside the member's roles: team:task.create."

    monkeypatch.setattr(settings, "BRAIN_TEAM_MODE", False)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_API_KEY", "")
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    assert auth.dev_open()
    assert await approve("", "", "", ["core:read"], "10.0.0.6") == (oauth_link.DEV_OPEN_SUBJECT, "")
    assert await approve("", "", "", ["team:read"], "10.0.0.7") == (
        "", "Team scopes require approval by a verified team member.")


@pytest.mark.asyncio
async def test_solo_brain_grants_personal_scopes_when_client_asks_for_every_advertised_scope(team_db, monkeypatch):
    """A connector that requests everything ``scopes_supported`` lists must still be authorized in solo
    state: there is no member to bind ``team:*`` to, so those scopes are dropped instead of denied."""
    factory, _, _ = team_db
    monkeypatch.setattr(mcp_oauth, "async_session_factory", factory)
    monkeypatch.setattr(auth, "async_session_factory", factory)
    monkeypatch.setattr(settings, "BRAIN_TEAM_MODE", False)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_API_KEY", "owner-key")  # production-like: not dev-open
    async with factory() as session, session.begin():
        await session.execute(update(TeamMember).values(revoked_at=datetime.now(timezone.utc)))
    await register_client(factory)
    provider = mcp_oauth.BrainOAuthProvider("http://localhost")
    # Registered without a scope: the registration default is every supported scope.
    client = OAuthClientInformationFull(client_id=CLIENT_ID, client_name="Reader",
                                        redirect_uris=["http://127.0.0.1:9999/callback"],
                                        scope=" ".join(mcp_oauth.SUPPORTED_SCOPES))
    params = AuthorizationParams(state="s", scopes=list(mcp_oauth.SUPPORTED_SCOPES), code_challenge="challenge",
                                 redirect_uri="http://127.0.0.1:9999/callback", redirect_uri_provided_explicitly=True)
    request_id = parse_qs(urlparse(await provider.authorize(client, params)).query)["request"][0]
    pending = provider.pending(request_id)
    assert pending.scopes and not oauth_link.team_scopes(pending.scopes)

    # Binding itself stays strict: approving team:* without a member is still refused.
    assert await mcp_oauth._approver("", "", "fixture-1", mcp_oauth.SUPPORTED_SCOPES, "10.0.0.8") == (
        "", "Team scopes require approval by a verified team member.")
    subject, error = await mcp_oauth._approver("", "", "fixture-1", pending.scopes, "10.0.0.9")
    assert (subject, error) == ("human-1", "")
    code = parse_qs(urlparse(provider.approve(request_id, subject)).query)["code"][0]
    token = await provider.exchange_authorization_code(client, provider._codes[code])
    assert set(token.scope.split()) == set(mcp_oauth.SUPPORTED_SCOPES) - set(oauth_link.TEAM_SCOPES)

    # Nothing personal left after dropping team scopes: fall back to what the tools need.
    params = params.model_copy(update={"scopes": ["team:read"]})
    request_id = parse_qs(urlparse(await provider.authorize(client, params)).query)["request"][0]
    assert provider.pending(request_id).scopes == mcp_oauth.DEFAULT_SCOPES


@pytest.mark.asyncio
async def test_team_brain_keeps_team_scopes_in_the_authorization_request(team_db, monkeypatch):
    factory, _, _ = team_db
    monkeypatch.setattr(mcp_oauth, "async_session_factory", factory)
    provider = mcp_oauth.BrainOAuthProvider("http://localhost")
    client = OAuthClientInformationFull(client_id=CLIENT_ID, client_name="Reader",
                                        redirect_uris=["http://127.0.0.1:9999/callback"])
    params = AuthorizationParams(state="s", scopes=["core:read", "team:read"], code_challenge="challenge",
                                 redirect_uri="http://127.0.0.1:9999/callback", redirect_uri_provided_explicitly=True)
    request_id = parse_qs(urlparse(await provider.authorize(client, params)).query)["request"][0]
    assert provider.pending(request_id).scopes == ["core:read", "team:read"]
