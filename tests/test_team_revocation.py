"""Revocation hardening in team state: revoking a member reaches every key acting for it, the
team never falls back to solo on its own, and the admin, web-login and Telegram surfaces cannot
be used to mint, keep or widen access around a project boundary. Every caller uses a real
member credential (``provisioning.member_scopes``), not the fixture's wildcard keys."""
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import select, update
from typer.testing import CliRunner

from apps.api import team_access
from apps.api.routers import admin, telegram_bridge, web_login
from apps.cli.commands import team as team_commands
from apps.cli.main import cli
from brain.auth.principals import ResolvedPrincipal
from brain.config.settings import settings
from brain.database.models import ApiCredential, OAuthClient, OAuthRefreshToken, Principal
from brain.team.authorization import team_state
from brain.team.models import AgentRun, TeamMember
from brain.team.provisioning import member_scopes
from brain.team.store import create_task, request_run
from tests.test_mcp_http import _free_port, _serve
from tests.test_team_foundations import caller, run_payload, task_payload, team_db as team_fixture
from tests.test_team_invitations import accept, invite, invite_app
from tests.test_team_transport import client_for, live_team_state, membership_driven, personal_app

team_db = team_fixture
pytestmark = pytest.mark.team_state
runner = CliRunner()
READER, CONTRIBUTOR, REVIEWER, ADMIN = ({"X-API-Key": f"fixture-{i}"} for i in (1, 2, 3, 4))
ADMIN_EXTRA = ["principals:read", "principals:write", "setup:read"]


async def use_member_keys(factory, *, admin_extra=ADMIN_EXTRA, member_extra=()):
    """Narrow the fixture's wildcard keys to the scopes a real member credential carries."""
    async with factory() as session, session.begin():
        for i in range(1, 5):
            extra = admin_extra if i == 4 else member_extra
            await session.execute(update(ApiCredential).where(ApiCredential.id == i)
                                  .values(scopes=sorted({*member_scopes(i == 4), *extra})))


def member(i, credential_id=None):
    """A resolved member identity matching ``use_member_keys``."""
    scopes = {*member_scopes(i == 4), *(ADMIN_EXTRA if i == 4 else ())}
    return ResolvedPrincipal(i, f"human-{i}", "human", scopes=frozenset(scopes), credential_id=credential_id or i)


def admin_app(factory, monkeypatch):
    app = personal_app(factory, monkeypatch)
    monkeypatch.setattr(admin, "async_session_factory", factory)
    app.include_router(admin.router)
    return app


def run_cli(factory, monkeypatch, *args):
    monkeypatch.setattr(team_commands, "async_session_factory", factory)
    monkeypatch.setattr(team_commands, "init_db", AsyncMock())
    return runner.invoke(cli, ["team", *args])


async def state(factory):
    async with factory() as session:
        return await team_state(session)


# 1. revoke_member revokes every credential, OAuth token and run of the principal.

async def seed_access(factory, a, principal_id=2, *, name=None, credential_id=None):
    """A second named key, an OAuth access token with its refresh token, and a run, all acting for ``principal_id``."""
    acting = ResolvedPrincipal(principal_id, name or f"human-{principal_id}", "human",
                               scopes=frozenset(member_scopes()), credential_id=credential_id or principal_id)
    async with factory() as session, session.begin():
        session.add(OAuthClient(client_id="synthetic-client", client_info={}))
        session.add(ApiCredential(id=20, principal_id=principal_id, key_hash="h" * 64, scopes=member_scopes()))
        session.add(ApiCredential(id=21, principal_id=principal_id, key_hash="o" * 64, scopes=["team:read"]))
        await session.flush()
        session.add(OAuthRefreshToken(token_hash="r" * 64, client_id="synthetic-client",
                                      principal_name=acting.name, scopes=["team:read"],
                                      access_credential_id=21))
        task = await create_task(session, acting, a, task_payload(f"seed-{principal_id}"))
        run = await request_run(session, acting, a, uuid.UUID(task["id"]),
                                run_payload(f"seed-run-{principal_id}"))
    return uuid.UUID(run["id"])


async def assert_all_access_revoked(factory, principal_id, run_id):
    async with factory() as session:
        own = (await session.scalars(select(ApiCredential).where(ApiCredential.principal_id == principal_id))).all()
        assert own and all(row.revoked_at for row in own)
        refresh = (await session.scalars(select(OAuthRefreshToken))).all()
        assert refresh and all(row.revoked_at for row in refresh)
        run = await session.get(AgentRun, run_id)
        assert run.revoked_at and (await session.get(ApiCredential, run.credential_id)).revoked_at
        others = await session.get(ApiCredential, 1)
        assert others.revoked_at is None


@pytest.mark.asyncio
async def test_api_member_revoke_revokes_credentials_oauth_tokens_and_runs(team_db, monkeypatch):
    factory, a, _ = team_db
    membership_driven(monkeypatch)
    await use_member_keys(factory)
    run_id = await seed_access(factory, a)
    async with client_for(personal_app(factory, monkeypatch)) as client:
        assert (await client.get(f"/team/projects/{a}/tasks", headers=CONTRIBUTOR)).status_code == 200
        revoked = await client.delete("/team/projects/members/2", headers=ADMIN)
        assert revoked.status_code == 200, revoked.text
        assert (await client.get(f"/team/projects/{a}/tasks", headers=CONTRIBUTOR)).status_code == 401
    await assert_all_access_revoked(factory, 2, run_id)


@pytest.mark.asyncio
async def test_invitation_revoke_revokes_every_key_of_the_invitee(team_db, monkeypatch):
    factory, a, _ = team_db
    membership_driven(monkeypatch)
    await use_member_keys(factory)
    async with client_for(invite_app(factory, monkeypatch)) as client:
        invitation_id, token = await invite(client, projects=[str(a)], role="contributor", name="invitee")
        accepted = (await accept(client, token)).json()
        principal_id = accepted["member"]["principal_id"]
        async with factory() as session:
            credential_id = (await session.scalars(select(ApiCredential.id).where(
                ApiCredential.principal_id == principal_id))).one()
        run_id = await seed_access(factory, a, principal_id, name="invitee", credential_id=credential_id)
        revoked = await client.post(f"/team/invitations/{invitation_id}/revoke", headers=ADMIN,
                                    json={"revoke_member": True})
        assert revoked.status_code == 200, revoked.text
        assert (await client.get(f"/team/projects/{a}/tasks",
                                 headers={"X-API-Key": accepted["credential"]})).status_code == 401
    await assert_all_access_revoked(factory, principal_id, run_id)


def test_cli_revoke_keeps_team_state_until_an_explicit_dissolve(team_db, monkeypatch):
    import asyncio

    factory, a, _ = team_db
    membership_driven(monkeypatch)
    asyncio.run(use_member_keys(factory))
    run_id = asyncio.run(seed_access(factory, a))
    revoked = run_cli(factory, monkeypatch, "member", "revoke", "--principal", "2")
    assert revoked.exit_code == 0, revoked.output
    asyncio.run(assert_all_access_revoked(factory, 2, run_id))

    for principal in ("1", "3"):
        assert run_cli(factory, monkeypatch, "member", "revoke", "--principal", principal).exit_code == 0
    live = run_cli(factory, monkeypatch, "dissolve", "--yes")
    assert live.exit_code == 1 and "Revoke every team member" in live.output
    assert run_cli(factory, monkeypatch, "member", "revoke", "--principal", "4").exit_code == 0
    # Every member is revoked, yet boundaries stay on: solo would make any surviving key an administrator.
    assert asyncio.run(state(factory)) == "team"
    unconfirmed = run_cli(factory, monkeypatch, "dissolve")
    assert unconfirmed.exit_code == 1 and "--yes" in unconfirmed.output
    assert asyncio.run(state(factory)) == "team"
    dissolved = run_cli(factory, monkeypatch, "dissolve", "--yes")
    assert dissolved.exit_code == 0, dissolved.output
    assert json.loads(dissolved.output)["dissolved_at"]
    assert asyncio.run(state(factory)) == "solo"


@pytest.mark.asyncio
async def test_a_member_added_after_dissolve_turns_the_team_back_on(team_db, monkeypatch):
    factory, _, _ = team_db
    membership_driven(monkeypatch)
    from brain.team import provisioning

    now = datetime.now(timezone.utc)
    async with factory() as session, session.begin():
        await session.execute(update(TeamMember).values(revoked_at=now))
        await provisioning.dissolve_team(session)
    assert await state(factory) == "solo"
    async with factory() as session, session.begin():
        session.add(Principal(id=9, name="newcomer", kind="human"))
        await session.flush()
        await provisioning.add_member(session, principal=await session.get(Principal, 9), issuer="corp-sso",
                                      subject="newcomer@example.test")
    assert await state(factory) == "team"


# 2. Admin credential minting: no wildcard, only held scopes, never for another member.

@pytest.mark.asyncio
async def test_admin_mint_refuses_wildcard_unheld_scopes_and_other_members(team_db, monkeypatch):
    factory, _, _ = team_db
    membership_driven(monkeypatch)
    await use_member_keys(factory)
    async with client_for(admin_app(factory, monkeypatch)) as client:
        wildcard = await client.post("/admin/principals/4/credentials", headers=ADMIN, json={"scopes": ["*"]})
        assert wildcard.status_code == 422
        unheld = await client.post("/admin/principals/4/credentials", headers=ADMIN, json={"scopes": ["harness:write"]})
        assert unheld.status_code == 403 and "harness:write" in unheld.json()["detail"]
        other = await client.post("/admin/principals/1/credentials", headers=ADMIN, json={"scopes": ["team:read"]})
        assert other.status_code == 403 and "another team member" in other.json()["detail"]
        created = await client.post("/admin/principals", headers=ADMIN,
                                    json={"name": "ci-bot", "kind": "service", "scopes": ["harness:write"]})
        assert created.status_code == 403
        own = await client.post("/admin/principals/4/credentials", headers=ADMIN, json={"scopes": ["team:read"]})
        assert own.status_code == 201 and own.json()["credential"]["scopes"] == ["team:read"]
        bot = await client.post("/admin/principals", headers=ADMIN,
                                json={"name": "ci-bot", "kind": "service", "scopes": ["team:read"]})
        assert bot.status_code == 201, bot.text
        bot_id = bot.json()["principal"]["id"]
        extra = await client.post(f"/admin/principals/{bot_id}/credentials", headers=ADMIN,
                                  json={"scopes": ["team:read"]})
        assert extra.status_code == 201
    async with factory() as session:
        assert all("*" not in row.scopes for row in (await session.scalars(
            select(ApiCredential).where(ApiCredential.id > 4))).all())


# 3. Admin disable/revoke: no self-lockout, never the last administrator credential.

@pytest.mark.asyncio
async def test_admin_cannot_disable_itself_or_revoke_the_credential_in_use(team_db, monkeypatch):
    factory, _, _ = team_db
    membership_driven(monkeypatch)
    await use_member_keys(factory)
    async with factory() as session, session.begin():
        session.add(ApiCredential(id=40, principal_id=4, key_hash="s" * 64, scopes=member_scopes(True)))
    async with client_for(admin_app(factory, monkeypatch)) as client:
        own_principal = await client.post("/admin/principals/4/disable", headers=ADMIN)
        assert own_principal.status_code == 409 and "own principal" in own_principal.json()["detail"]
        in_use = await client.post("/admin/credentials/4/revoke", headers=ADMIN)
        assert in_use.status_code == 409 and "using" in in_use.json()["detail"]
        spare = await client.post("/admin/credentials/40/revoke", headers=ADMIN)
        assert spare.status_code == 200 and spare.json()["credential"]["revoked_at"]
        assert (await client.post("/admin/principals/1/disable", headers=ADMIN)).status_code == 200
        assert (await client.get("/admin/principals", headers=ADMIN)).status_code == 200


@pytest.mark.asyncio
async def test_last_admin_guard_refuses_removing_the_only_administrator(team_db, monkeypatch):
    factory, _, _ = team_db
    membership_driven(monkeypatch)
    await use_member_keys(factory)
    async with factory() as session, session.begin():
        await session.execute(update(TeamMember).where(TeamMember.principal_id == 3).values(is_admin=True))
        await session.execute(update(ApiCredential).where(ApiCredential.id == 3).values(scopes=member_scopes(True)))
    # Two administrators: either may disable the other.
    async with factory() as session, session.begin():
        await admin._keep_an_administrator(session, member(4), principal_id=3)
    # Principal 3 is the only one left holding team:admin; nobody may take it away (e.g. a concurrent request
    # that raced principal 4's own removal), whoever asks.
    async with factory() as session, session.begin():
        await session.execute(update(ApiCredential).where(ApiCredential.id == 4).values(scopes=member_scopes()))
    for target in ({"principal_id": 3}, {"credential_id": 3}):
        async with factory() as session, session.begin():
            with pytest.raises(HTTPException) as refused:
                await admin._keep_an_administrator(session, member(4), **target)
        assert refused.value.status_code == 409 and "without a live administrator" in refused.value.detail


# 4. web-login: only the team administrator may read the login or verify passwords.

@pytest.mark.asyncio
async def test_web_login_read_and_verify_need_the_team_administrator(team_db, monkeypatch, tmp_path):
    factory, _, _ = team_db
    membership_driven(monkeypatch)
    monkeypatch.chdir(tmp_path)
    for key in web_login.KEYS:
        monkeypatch.setattr(settings, key, None)
    web_login._failures.clear()
    # The member holds setup:read, so only the administrator guard can refuse it.
    await use_member_keys(factory, member_extra=["setup:read"])
    app = personal_app(factory, monkeypatch)
    app.include_router(web_login.router)
    probe = {"user": "owner", "password": "guess"}
    async with client_for(app) as client:
        assert (await client.get("/api/web-login", headers=READER)).status_code == 403
        assert (await client.post("/api/web-login/verify", headers=READER, json=probe)).status_code == 403
        assert (await client.get("/api/web-login", headers=ADMIN)).status_code == 200
        verified = await client.post("/api/web-login/verify", headers=ADMIN, json=probe)
        assert verified.status_code == 200 and verified.json()["ok"] is False
        monkeypatch.setattr(team_access, "current_team_state", AsyncMock(return_value="solo"))
        assert (await client.get("/api/web-login", headers=READER)).status_code == 200
    web_login._failures.clear()


# 5. Telegram: in team state a repository prefix only resolves to repositories the poller key reads.

@pytest.mark.asyncio
async def test_telegram_resolve_repo_only_sees_repositories_the_poller_reads(team_db, monkeypatch):
    factory, _, _ = team_db
    membership_driven(monkeypatch)
    await use_member_keys(factory)
    for module in (telegram_bridge, team_access):
        monkeypatch.setattr(module, "async_session_factory", factory)
    monkeypatch.setattr(team_access, "current_team_state", live_team_state)
    monkeypatch.setattr(settings, "BRAIN_TELEGRAM_DEFAULT_REPO", None)
    poller = member(1)  # reader on project A only
    assert await telegram_bridge._resolve_repo("A: what changed?", None, poller) == ("/synthetic/A", "what changed?")
    assert await telegram_bridge._resolve_repo("B: what changed?", None, poller) == (None, "B: what changed?")
    assert await telegram_bridge._resolve_repo("hello", "b", poller) == (None, "hello")
    monkeypatch.setattr(team_access, "current_team_state", AsyncMock(return_value="solo"))
    assert await telegram_bridge._resolve_repo("B: what changed?", None, poller) == ("/synthetic/B", "what changed?")


# End to end over REST and native MCP with the invitee's own key.

@pytest.mark.asyncio
async def test_invite_accept_read_deny_revoke_deny_over_rest_and_mcp(team_db, monkeypatch):
    factory, a, b = team_db
    membership_driven(monkeypatch)
    await use_member_keys(factory)
    async with factory() as session, session.begin():
        await create_task(session, caller(2), a, task_payload())
    app = invite_app(factory, monkeypatch)
    with _serve(app, _free_port()) as url:
        async with httpx.AsyncClient(base_url=url, timeout=30) as rest:
            created = await rest.post("/team/invitations", headers=ADMIN,
                                      json={"projects": [str(a)], "role": "reader", "name": "erin"})
            assert created.status_code == 200, created.text
            accepted = await rest.post("/team/invitations/accept", json={"token": created.json()["token"]})
            assert accepted.status_code == 200, accepted.text
            key, principal_id = accepted.json()["credential"], accepted.json()["member"]["principal_id"]
            headers = {"X-API-Key": key}
            read = await rest.get(f"/team/projects/{a}/tasks", headers=headers)
            assert read.status_code == 200 and len(read.json()) == 1
            assert (await rest.get(f"/team/projects/{b}/tasks", headers=headers)).status_code == 403

            bearer = {"Authorization": f"Bearer {key}"}
            async with httpx.AsyncClient(headers=bearer, timeout=30) as http_client:
                async with Client(streamable_http_client(f"{url}/mcp", http_client=http_client)) as client:
                    allowed = await client.call_tool("team_list_tasks", {"project_id": str(a)})
                    assert not allowed.is_error, allowed.content
                    assert json.loads(allowed.content[0].text)["author_id"] == 2
                    assert (await client.call_tool("team_list_tasks", {"project_id": str(b)})).is_error

            revoked = await rest.delete(f"/team/projects/members/{principal_id}", headers=ADMIN)
            assert revoked.status_code == 200, revoked.text
            assert (await rest.get(f"/team/projects/{a}/tasks", headers=headers)).status_code == 401
            assert (await rest.post(f"{url}/mcp", headers=bearer, json={})).status_code == 401
    assert await state(factory) == "team"
