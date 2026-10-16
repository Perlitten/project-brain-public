"""Team invitations: invited -> verified -> active -> revoked over the core, HTTP and CLI surfaces."""
import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
import httpx
import pytest
from sqlalchemy import func, select, update
from typer.testing import CliRunner

from apps.api.audit_middleware import AuditLogMiddleware
from apps.api.routers import team_invitations
from apps.cli.commands import team as team_commands
from apps.cli.main import cli
from brain.auth.principals import hash_credential
from brain.config.settings import settings
from brain.database.models import ApiCredential, Principal
from brain.team import invitations
from brain.team.authorization import TeamConflict
from brain.team.models import ProjectMember, TeamInvitation, TeamMember
from brain.team.provisioning import revoke_member
from tests.test_team_foundations import team_db as team_fixture
from tests.test_team_transport import client_for, live_team_state, membership_driven, personal_app, revoke_everyone

team_db = team_fixture
pytestmark = pytest.mark.team_state
runner = CliRunner()
ADMIN, READER = {"X-API-Key": "fixture-4"}, {"X-API-Key": "fixture-1"}
LEGACY = {"X-API-Key": "fixture-legacy"}


@pytest.fixture(autouse=True)
def _fresh_rate_limit():
    team_invitations._attempts.clear()
    yield
    team_invitations._attempts.clear()


def invite_app(factory, monkeypatch):
    app = personal_app(factory, monkeypatch)
    monkeypatch.setattr(team_invitations, "async_session_factory", factory)
    app.include_router(team_invitations.router)
    return app


async def invite(client, headers=ADMIN, **body):
    response = await client.post("/team/invitations", headers=headers, json=body)
    assert response.status_code == 200, response.text
    payload = response.json()
    return payload["invitation"]["id"], payload["token"]


async def accept(client, token):
    return await client.post("/team/invitations/accept", json={"token": token})


async def states(client, headers=ADMIN):
    response = await client.get("/team/invitations", headers=headers)
    assert response.status_code == 200, response.text
    return {row["id"]: row["state"] for row in response.json()}


@pytest.mark.asyncio
async def test_create_accept_retry_and_activation(team_db, monkeypatch):
    factory, a, _ = team_db
    app = invite_app(factory, monkeypatch)
    async with client_for(app) as client:
        created = await client.post("/team/invitations", headers=ADMIN, json={
            "projects": [str(a)], "role": "reader", "name": "alice", "email": "alice@example.test"})
        assert created.status_code == 200 and created.headers["cache-control"] == "no-store"
        invitation_id, token = created.json()["invitation"]["id"], created.json()["token"]
        assert token.startswith("pbi_") and created.json()["invitation"]["state"] == "invited"
        listing = (await client.get("/team/invitations", headers=ADMIN)).json()
        assert listing[0]["invited_by"] == "principal:4" and listing[0]["invited_by_principal_id"] == 4
        assert token not in json.dumps(listing) and hash_credential(token) not in json.dumps(listing)

        first = await accept(client, token)
        assert first.status_code == 200 and first.headers["cache-control"] == "no-store"
        body = first.json()
        key = body["credential"]
        assert key.startswith("pbk_") and body["invitation"]["state"] == "verified"
        assert body["member"]["principal_name"] == "alice" and not body["member"]["is_admin"]
        assert body["member"]["verification_issuer"] == "brain-invitation"
        assert body["member"]["verification_subject"] == f"invitation:{invitation_id}"
        assert [row["role"] for row in body["project_memberships"]] == ["reader"]

        retry = await accept(client, token)
        assert retry.status_code == 200 and retry.json()["credential"] is None
        assert retry.json()["member"]["principal_id"] == body["member"]["principal_id"]

        member = {"X-API-Key": key}
        assert (await client.get(f"/team/projects/{a}/tasks", headers=member)).status_code == 200
        assert (await states(client))[invitation_id] == "active"
        # The role is the project role: a reader cannot create tasks, and a non-admin cannot invite.
        task = {"repository_id": 1, "title": "t", "goal": "g", "branch": "main", "base_sha": "a" * 40,
                "idempotency_key": "invitee-task-1"}
        assert (await client.post(f"/team/projects/{a}/tasks", headers=member, json=task)).status_code == 403
        assert (await client.post("/team/invitations", headers=member, json={"is_admin": True})).status_code == 403
    async with factory() as session:
        row = await session.get(TeamInvitation, uuid.UUID(invitation_id))
        assert row.token_hash == hash_credential(token) and row.accepted_at and row.email == "alice@example.test"
        credential = await session.get(ApiCredential, row.accepted_credential_id)
        assert "team:admin" not in credential.scopes and credential.key_hash == hash_credential(key)
        count = (await session.execute(select(func.count()).select_from(ApiCredential)
                                       .where(ApiCredential.principal_id == row.accepted_principal_id))).scalar_one()
        assert count == 1


@pytest.mark.asyncio
async def test_successful_accept_is_attributed_to_the_invitee_in_the_audit_log(team_db, monkeypatch):
    factory, _, _ = team_db
    app = invite_app(factory, monkeypatch)
    audit = AsyncMock()
    monkeypatch.setattr("apps.api.audit_middleware.record_audit_event", audit)
    app.add_middleware(AuditLogMiddleware)
    async with client_for(app) as client:
        _, token = await invite(client, name="audited-invitee")
        audit.reset_mock()
        accepted = await accept(client, token)
        assert accepted.status_code == 200
    assert audit.await_count == 1
    recorded = audit.await_args.kwargs
    assert recorded["path"] == "/team/invitations/accept"
    assert recorded["principal_name"] == "audited-invitee"
    assert recorded["principal_id"] == accepted.json()["member"]["principal_id"]
    assert recorded["credential_id"] is not None


@pytest.mark.asyncio
async def test_expired_unknown_and_revoked_tokens_get_one_answer(team_db, monkeypatch):
    factory, a, _ = team_db
    app = invite_app(factory, monkeypatch)
    async with client_for(app) as client:
        expiring, expired_token = await invite(client, projects=[str(a)], expires_hours=1)
        async with factory() as session, session.begin():
            await session.execute(update(TeamInvitation).where(TeamInvitation.id == uuid.UUID(expiring))
                                  .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
        pending, pending_token = await invite(client, projects=[str(a)])
        revoked = await client.post(f"/team/invitations/{pending}/revoke", headers=ADMIN)
        assert revoked.status_code == 200 and revoked.json()["state"] == "revoked"
        assert revoked.json()["revoked_by"] == "principal:4"
        assert (await client.post(f"/team/invitations/{pending}/revoke", headers=ADMIN)).status_code == 200
        for token in (expired_token, pending_token, "pbi_" + "x" * 43, "not-an-invitation-token"):
            response = await accept(client, token)
            assert response.status_code == 403 and response.json()["detail"] == invitations.REJECTED
        assert await states(client) == {expiring: "expired", pending: "revoked"}
        missing = await client.post(f"/team/invitations/{uuid.uuid4()}/revoke", headers=ADMIN)
        assert missing.status_code == 404
    async with factory() as session:
        assert (await session.execute(select(Principal).where(Principal.name.like("invitee-%")))).first() is None


@pytest.mark.asyncio
async def test_revoking_an_accepted_invitation_revokes_the_member_explicitly(team_db, monkeypatch):
    factory, a, _ = team_db
    app = invite_app(factory, monkeypatch)
    async with client_for(app) as client:
        invitation_id, token = await invite(client, projects=[str(a)], role="contributor", name="bob")
        key = (await accept(client, token)).json()["credential"]
        refused = await client.post(f"/team/invitations/{invitation_id}/revoke", headers=ADMIN, json={})
        assert refused.status_code == 409 and "revoke_member" in refused.json()["detail"]
        assert (await client.get(f"/team/projects/{a}/tasks", headers={"X-API-Key": key})).status_code == 200
        revoked = await client.post(f"/team/invitations/{invitation_id}/revoke", headers=ADMIN,
                                    json={"revoke_member": True})
        assert revoked.status_code == 200 and revoked.json()["state"] == "revoked"
        assert (await client.get(f"/team/projects/{a}/tasks", headers={"X-API-Key": key})).status_code == 401
        assert (await accept(client, token)).status_code == 403
    async with factory() as session:
        row = await session.get(TeamInvitation, uuid.UUID(invitation_id))
        member = await session.get(TeamMember, row.accepted_principal_id)
        role = await session.get(ProjectMember, (a, row.accepted_principal_id))
        credential = await session.get(ApiCredential, row.accepted_credential_id)
        assert member.revoked_at and role.revoked_at and credential.revoked_at and row.revoked_at


@pytest.mark.asyncio
async def test_administrators_cannot_revoke_their_own_accepted_invitation(team_db):
    factory, _, _ = team_db
    async with factory() as session, session.begin():
        token, created = await invitations.create_invitation(session, invited_by="principal:4", is_admin=True,
                                                             name="second-admin")
        accepted = await invitations.accept_invitation(session, token)
        principal_id = accepted["member"]["principal_id"]
        assert accepted["member"]["is_admin"] and accepted["credential"]
        with pytest.raises(TeamConflict, match="own membership"):
            await invitations.revoke_invitation(session, uuid.UUID(created["id"]), revoked_by="x",
                                                revoke_issued_member=True, protected_principal_id=principal_id)
        credential = await session.get(ApiCredential, (await session.get(TeamInvitation, uuid.UUID(created["id"])))
                                       .accepted_credential_id)
        assert "team:admin" in credential.scopes


@pytest.mark.asyncio
async def test_accepted_invitation_revocation_is_never_silent(team_db):
    factory, a, _ = team_db
    async with factory() as session, session.begin():
        token, created = await invitations.create_invitation(session, invited_by="principal:4", name="carol",
                                                             projects=[str(a)], invited_by_principal_id=4)
        accepted = await invitations.accept_invitation(session, token)
        principal = await session.get(Principal, accepted["member"]["principal_id"])
        await revoke_member(session, principal=principal)
        # The member is already revoked elsewhere, yet the invitation still needs the explicit flag.
        with pytest.raises(TeamConflict, match="revoke_member"):
            await invitations.revoke_invitation(session, uuid.UUID(created["id"]), revoked_by="principal:4")
        revoked = await invitations.revoke_invitation(session, uuid.UUID(created["id"]), revoked_by="principal:4",
                                                      revoke_issued_member=True)
        again = await invitations.revoke_invitation(session, uuid.UUID(created["id"]), revoked_by="someone-else")
        assert revoked["state"] == again["state"] == "revoked" and again["revoked_by"] == "principal:4"
        row = await session.get(TeamInvitation, uuid.UUID(created["id"]))
        assert (await session.get(ApiCredential, row.accepted_credential_id)).revoked_at is not None


@pytest.mark.asyncio
async def test_invitation_dies_with_its_inviters_administration(team_db, monkeypatch):
    factory, a, _ = team_db
    app = invite_app(factory, monkeypatch)
    async with client_for(app) as client:
        _, token = await invite(client, projects=[str(a)], name="dave")
        async with factory() as session, session.begin():
            await session.execute(update(TeamMember).where(TeamMember.principal_id == 4).values(is_admin=False))
        refused = await accept(client, token)
        assert refused.status_code == 409 and "no longer administers" in refused.json()["detail"]
    async with factory() as session:
        assert (await session.execute(select(Principal).where(Principal.name == "dave"))).first() is None


@pytest.mark.asyncio
async def test_non_admins_dev_open_and_unscoped_credentials_cannot_invite(team_db, monkeypatch):
    factory, a, _ = team_db
    app = invite_app(factory, monkeypatch)
    async with client_for(app) as client:
        invitation_id, _ = await invite(client, projects=[str(a)])
        assert (await client.post("/team/invitations", headers=READER, json={})).status_code == 403
        assert (await client.get("/team/invitations", headers=READER)).status_code == 403
        assert (await client.post(f"/team/invitations/{invitation_id}/revoke", headers=READER)).status_code == 403
    membership_driven(monkeypatch)
    await revoke_everyone(factory)
    async with factory() as session, session.begin():
        session.add(Principal(id=5, name="service-5", kind="service"))
        await session.flush()
        session.add(ApiCredential(id=5, principal_id=5, key_hash=hash_credential("fixture-5"), scopes=["jobs:write"]))
    monkeypatch.setattr(settings, "PROJECT_BRAIN_API_KEY", "")
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    async with client_for(app) as client:
        # A team of one has no administrator member: nobody, not even dev-open
        # or a credential without team scopes, issues invitations over the API.
        for headers in ({}, {"X-API-Key": "fixture-5"}):
            refused = await client.post("/team/invitations", headers=headers, json={"is_admin": True})
            assert refused.status_code == 409 and "no team members yet" in refused.json()["detail"]
    async with factory() as session:
        assert (await session.execute(select(func.count()).select_from(TeamInvitation))).scalar_one() == 1


@pytest.mark.asyncio
async def test_validation_and_role_narrowing(team_db, monkeypatch):
    factory, a, b = team_db
    app = invite_app(factory, monkeypatch)
    async with client_for(app) as client:
        for body in ({"role": "owner"}, {"expires_hours": 0}, {"expires_hours": 721}, {"token": "x"},
                     {"projects": [str(a)] * 17}):
            assert (await client.post("/team/invitations", headers=ADMIN, json=body)).status_code == 422, body
        assert (await client.post("/team/invitations", headers=ADMIN, json={"projects": ["ghost"]})).status_code == 404
        for body in ({"name": "human-1"}, {"name": "1234"}, {"name": "has space"}, {"email": "not-an-email"}):
            assert (await client.post("/team/invitations", headers=ADMIN, json=body)).status_code == 409, body
        await invite(client, name="carol", projects=[str(a), str(b)], role="reviewer")
        duplicate = await client.post("/team/invitations", headers=ADMIN, json={"name": "carol"})
        assert duplicate.status_code == 409 and "pending" in duplicate.json()["detail"]
        assert (await client.post("/team/invitations/accept", json={"token": "x"})).status_code == 422


@pytest.mark.asyncio
async def test_accept_is_rate_limited_per_client(team_db, monkeypatch):
    factory, a, _ = team_db
    app = invite_app(factory, monkeypatch)
    monkeypatch.setattr(team_invitations, "ACCEPT_LIMIT", 3)
    async with client_for(app) as client:
        _, token = await invite(client, projects=[str(a)])
        for _ in range(3):
            assert (await accept(client, "pbi_" + "y" * 43)).status_code == 403
        throttled = await accept(client, token)
        assert throttled.status_code == 429
    async with factory() as session:
        assert (await session.execute(select(TeamInvitation.accepted_at))).scalar_one() is None


@pytest.mark.asyncio
async def test_first_accepted_invitation_turns_a_solo_brain_into_a_team(team_db, monkeypatch):
    factory, a, _ = team_db
    membership_driven(monkeypatch)
    await revoke_everyone(factory)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_API_KEY", "fixture-legacy")
    app = invite_app(factory, monkeypatch)
    async with client_for(app) as client:
        # The shared key would be retired by the first acceptance, so it cannot invite over the API.
        refused = await client.post("/team/invitations", headers=LEGACY, json={"is_admin": True, "name": "owner"})
        assert refused.status_code == 409 and "brain team invite create --admin" in refused.json()["detail"]
        assert (await client.get("/team/invitations", headers=LEGACY)).status_code == 409
        # The operator bootstraps on the server; the first member must administer the team.
        async with factory() as session:
            with pytest.raises(TeamConflict, match="must grant team administration"):
                async with session.begin():
                    await invitations.create_invitation(session, invited_by="operator:cli", projects=[str(a)])
        async with factory() as session, session.begin():
            token, created = await invitations.create_invitation(session, invited_by="operator:cli", is_admin=True,
                                                                 name="owner", projects=[str(a)])
        assert await live_team_state() == "solo"
        accepted = await accept(client, token)
        assert accepted.status_code == 200 and accepted.json()["member"]["is_admin"]
        assert await live_team_state() == "team"
        owner = {"X-API-Key": accepted.json()["credential"]}
        refused = await client.get("/team/invitations", headers=LEGACY)
        assert refused.status_code == 403 and "Named credential required" in refused.json()["detail"]
        assert (await client.get("/team/projects/", headers=owner)).status_code == 200
        listing = (await client.get("/team/invitations", headers=owner)).json()
        assert [(row["id"], row["state"], row["invited_by"]) for row in listing] ==             [(created["id"], "active", "operator:cli")]
        await invite(client, headers=owner, projects=[str(a)], role="reader")


@pytest.mark.asyncio
async def test_non_admin_invitation_cannot_be_the_first_member(team_db, monkeypatch):
    factory, a, _ = team_db
    membership_driven(monkeypatch)
    async with factory() as session, session.begin():
        token, created = await invitations.create_invitation(session, invited_by="principal:4", projects=[str(a)])
    await revoke_everyone(factory)
    async with factory() as session:
        with pytest.raises(TeamConflict, match="must grant administration"):
            async with session.begin():
                await invitations.accept_invitation(session, token)
    async with factory() as session:
        assert (await session.get(TeamInvitation, uuid.UUID(created["id"]))).accepted_at is None


def test_cli_invite_lifecycle(team_db, monkeypatch):
    factory, a, _ = team_db
    monkeypatch.setattr(team_commands, "async_session_factory", factory)
    monkeypatch.delenv("BRAIN_API_URL", raising=False)

    def invoke(*args):
        return runner.invoke(cli, ["team", "invite", *args])

    created = invoke("create", "--project", str(a), "--role", "contributor", "--name", "dave",
                     "--email", "dave@example.test", "--expires-hours", "24")
    assert created.exit_code == 0, created.output
    stdout, _, token_block = created.output.partition("Invitation token")
    invitation = json.loads(stdout)
    token = token_block.strip().splitlines()[-1]
    assert token.startswith("pbi_") and invitation["invited_by"] == "operator:cli"
    listing = invoke("list")
    assert [row["state"] for row in json.loads(listing.output)] == ["invited"] and token not in listing.output
    accepted = invoke("accept", token)
    assert accepted.exit_code == 0, accepted.output
    result, _, key_block = accepted.output.partition("New member credential")
    assert json.loads(result)["member"]["principal_name"] == "dave"
    assert key_block.strip().splitlines()[-1].startswith("pbk_")
    again = invoke("accept", token)
    assert again.exit_code == 0 and "already accepted" in again.output and "pbk_" not in again.output
    refused = invoke("revoke", invitation["id"])
    assert refused.exit_code == 1 and "revoke_member" in refused.output
    revoked = invoke("revoke", invitation["id"], "--revoke-member")
    assert revoked.exit_code == 0 and json.loads(revoked.output)["state"] == "revoked"
    assert invoke("accept", token).exit_code == 1
    assert invoke("revoke", "not-a-uuid").exit_code == 1
    insecure = invoke("accept", token, "--api-url", "http://brain.example.test")
    assert insecure.exit_code == 1 and "https" in insecure.output
    prompted = runner.invoke(cli, ["team", "invite", "accept"], input="pbi_unknown\n")
    assert prompted.exit_code == 1 and invitations.REJECTED in prompted.output


def test_cli_accept_against_a_remote_api(monkeypatch):
    calls = []

    def post(url, json, timeout):
        calls.append((url, json))
        return httpx.Response(200, json={"credential": "pbk_remote", "member": {"principal_name": "erin"}},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(team_commands.httpx, "post", post)
    result = runner.invoke(cli, ["team", "invite", "accept", "pbi_token", "--api-url", "https://brain.example.test/"])
    assert result.exit_code == 0, result.output
    assert calls == [("https://brain.example.test/team/invitations/accept", {"token": "pbi_token"})]
    assert "pbk_remote" in result.output

    def denied(url, json, timeout):
        return httpx.Response(403, json={"detail": invitations.REJECTED}, request=httpx.Request("POST", url))

    monkeypatch.setattr(team_commands.httpx, "post", denied)
    result = runner.invoke(cli, ["team", "invite", "accept", "pbi_token", "--api-url", "http://127.0.0.1:8000"])
    assert result.exit_code == 1 and invitations.REJECTED in result.output
    for unsafe in ("https://brain.example.test/base?tenant=x", "https://brain.example.test/base#fragment"):
        result = runner.invoke(cli, ["team", "invite", "accept", "pbi_token", "--api-url", unsafe])
        assert result.exit_code == 1 and "https" in result.output


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv("TEST_TEAM_POSTGRES_URL"), reason="row-lock concurrency needs PostgreSQL")
async def test_postgres_concurrent_accepts_issue_exactly_one_member(team_db):
    factory, a, _ = team_db
    async with factory() as session, session.begin():
        token, created = await invitations.create_invitation(session, invited_by="principal:4", projects=[str(a)],
                                                             name="frank")

    async def attempt():
        async with factory() as session, session.begin():
            return await invitations.accept_invitation(session, token)

    results = await asyncio.gather(*(attempt() for _ in range(4)))
    assert sum(1 for result in results if result["credential"]) == 1
    assert len({result["member"]["principal_id"] for result in results}) == 1
    async with factory() as session:
        row = await session.get(TeamInvitation, uuid.UUID(created["id"]))
        credentials = (await session.execute(select(func.count()).select_from(ApiCredential)
                                             .where(ApiCredential.principal_id == row.accepted_principal_id))).scalar_one()
        principals = (await session.execute(select(func.count()).select_from(Principal)
                                            .where(Principal.name == "frank"))).scalar_one()
        assert credentials == 1 and principals == 1


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv("TEST_TEAM_POSTGRES_URL"), reason="row-lock concurrency needs PostgreSQL")
async def test_postgres_concurrent_creates_issue_one_pending_invitation(team_db):
    factory, _, _ = team_db

    async def attempt():
        try:
            async with factory() as session, session.begin():
                return await invitations.create_invitation(
                    session, invited_by="principal:4", name="one-pending-token"
                )
        except TeamConflict as exc:
            return exc

    results = await asyncio.gather(attempt(), attempt())
    assert sum(isinstance(result, tuple) for result in results) == 1
    assert sum(isinstance(result, TeamConflict) for result in results) == 1
    async with factory() as session:
        count = (await session.execute(
            select(func.count()).select_from(TeamInvitation).where(
                TeamInvitation.principal_name == "one-pending-token",
                TeamInvitation.accepted_at.is_(None),
                TeamInvitation.revoked_at.is_(None),
            )
        )).scalar_one()
        assert count == 1
