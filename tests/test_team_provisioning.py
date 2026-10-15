"""Team provisioning: idempotent primitives, admin HTTP routes and the operator CLI."""
import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from typer.testing import CliRunner

from apps.cli.commands import team as team_commands
from apps.cli.main import cli
from brain.auth.principals import ResolvedPrincipal, hash_credential
from brain.config.settings import settings
from brain.database.models import ApiCredential, Principal
from brain.team import provisioning
from brain.team.authorization import Permission, TeamConflict, TeamDenied, team_administrator
from brain.team.models import ProjectMember, Team, TeamMember, TeamProject
from brain.team.provisioning import TeamNotFound
from tests.test_team_foundations import caller, team_db as team_fixture
from tests.test_team_transport import team_app

team_db = team_fixture
runner = CliRunner()
ADMIN = {"X-API-Key": "fixture-4"}


@pytest.mark.asyncio
async def test_init_team_is_idempotent_and_refuses_renames(tmp_path, team_db):
    factory, _, _ = team_db
    async with factory() as session, session.begin():
        team = await provisioning.init_team(session, "synthetic team")
        assert team.id == 1
        with pytest.raises(TeamConflict, match="already initialized"):
            await provisioning.init_team(session, "another team")
        assert (await session.execute(select(Team))).scalars().all() == [team]


@pytest.mark.asyncio
async def test_member_lifecycle_requires_verified_humans(team_db):
    factory, a, _ = team_db
    async with factory() as session, session.begin():
        session.add_all([Principal(id=10, name="newcomer", kind="human"), Principal(id=11, name="bot", kind="service")])
        await session.flush()
        human = await provisioning.resolve_principal(session, "newcomer")
        member = await provisioning.add_member(session, principal=human, issuer="corp-sso", subject="n@example.test")
        again = await provisioning.add_member(session, principal=human, issuer="corp-sso", subject="n@example.test")
        assert again is member and member.verified_at and member.revoked_at is None and not member.is_admin
        with pytest.raises(TeamConflict, match="different issuer/subject"):
            await provisioning.add_member(session, principal=human, issuer="corp-sso", subject="other")
        with pytest.raises(TeamConflict, match="must be human"):
            await provisioning.add_member(session, principal=await provisioning.resolve_principal(session, 11),
                                          issuer="corp-sso", subject="bot")
        other = await provisioning.resolve_principal(session, 1)
        with pytest.raises(TeamConflict, match="belongs to another member"):
            await provisioning.add_member(session, principal=other, issuer="corp-sso", subject="n@example.test")
        membership = await provisioning.assign_project_member(session, project_id=a, principal_id=10, role="reader")
        assert (membership.role, membership.version) == ("reader", 0)
        assert await provisioning.assign_project_member(session, project_id=a, principal_id=10, role="reader") is membership
        assert membership.version == 0
        changed = await provisioning.assign_project_member(session, project_id=a, principal_id=10, role="reviewer")
        assert (changed.role, changed.version) == ("reviewer", 1)
        with pytest.raises(TeamConflict, match="Unknown project role"):
            await provisioning.assign_project_member(session, project_id=a, principal_id=10, role="owner")
    async with factory() as session, session.begin():
        revoked = await provisioning.revoke_member(session, principal=await provisioning.resolve_principal(session, 10))
        assert revoked.revoked_at is not None
        project_membership = await session.get(ProjectMember, (a, 10))
        assert project_membership.revoked_at == revoked.revoked_at and project_membership.version == 2
        with pytest.raises(TeamConflict, match="not a verified, active team member"):
            await provisioning.assign_project_member(session, project_id=a, principal_id=10, role="reader")
        reactivated = await provisioning.add_member(session, principal=await provisioning.resolve_principal(session, 10),
                                                    issuer="corp-sso", subject="n@example.test", is_admin=True)
        assert reactivated.revoked_at is None and reactivated.is_admin
        with pytest.raises(TeamNotFound):
            await provisioning.revoke_member(session, principal=await provisioning.resolve_principal(session, 11))


@pytest.mark.asyncio
async def test_projects_and_repositories_are_idempotent(team_db):
    factory, a, b = team_db
    async with factory() as session, session.begin():
        project = await provisioning.create_project(session, name="docs")
        assert sorted(project.permissions) == sorted(str(p) for p in Permission)
        assert (await provisioning.create_project(session, name="docs")).id == project.id
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                session.add(TeamProject(team_id=1, name="docs", permissions=[]))
                await session.flush()
        with pytest.raises(TeamConflict, match="different permission policy"):
            await provisioning.create_project(session, name="docs", allowed=["read"])
        with pytest.raises(TeamConflict, match="Invalid authority policy"):
            await provisioning.create_project(session, name="broken", allowed=["fly"])
        uuid_name = "123e4567-e89b-12d3-a456-426614174000"
        named_like_uuid = await provisioning.create_project(session, name=uuid_name)
        assert await provisioning.resolve_project(session, uuid_name) is named_like_uuid
        assert (await provisioning.resolve_project(session, "docs")).id == project.id
        assert (await provisioning.resolve_project(session, str(project.id))).id == project.id
        with pytest.raises(TeamNotFound):
            await provisioning.resolve_project(session, "missing")
        repo = await provisioning.resolve_repository(session, "/synthetic/A")
        link = await provisioning.attach_repository(session, project_id=a, repository=repo)
        assert link.project_id == a
        with pytest.raises(TeamConflict, match="another project"):
            await provisioning.attach_repository(session, project_id=b, repository=repo)
        with pytest.raises(TeamNotFound, match="index it first"):
            await provisioning.resolve_repository(session, "/synthetic/missing")
        assert [row["repository_id"] for row in await provisioning.list_project_repositories(session, a)] == [1]
        names = [row["name"] for row in await provisioning.list_projects(session)]
        assert {"docs", uuid_name}.issubset(names) and len(names) == 4


@pytest.mark.asyncio
async def test_team_administrator_requires_admin_flag_and_scope(team_db):
    factory, _, _ = team_db
    async with factory() as session, session.begin():
        # Scopes come from the live credential row, never from the request-time principal.
        session.add(ApiCredential(id=40, principal_id=4, key_hash=hash_credential("fixture-4-narrow"),
                                  scopes=["team:read"]))
    async with factory() as session:
        member = await team_administrator(session, caller(4))
        assert member.principal_id == 4
        narrow = ResolvedPrincipal(4, "human-4", "human", scopes=frozenset({"*"}), credential_id=40)
        for denied in (caller(3), narrow):
            with pytest.raises(TeamDenied):
                await team_administrator(session, denied)


@pytest.mark.asyncio
async def test_admin_routes_provision_without_sql(team_db, monkeypatch):
    factory, a, b = team_db
    app = team_app(factory, monkeypatch)
    async with factory() as session, session.begin():
        session.add(Principal(id=20, name="new-human", kind="human"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        # Non-admin members and unauthenticated callers never reach provisioning.
        assert (await client.get("/team/projects/")).status_code == 401
        assert (await client.get("/team/projects/", headers={"X-API-Key": "fixture-3"})).status_code == 403
        assert (await client.post("/team/projects/members", headers={"X-API-Key": "fixture-3"},
                                  json={"principal_id": 20})).status_code == 403
        created = await client.post("/team/projects/", headers=ADMIN, json={"name": "docs", "permissions": ["read"]})
        assert created.status_code == 200 and created.json()["permissions"] == ["read"]
        repeat = await client.post("/team/projects/", headers=ADMIN, json={"name": "docs", "permissions": ["read"]})
        assert repeat.json()["id"] == created.json()["id"]
        conflict = await client.post("/team/projects/", headers=ADMIN, json={"name": "docs"})
        assert conflict.status_code == 409
        invalid_policy = await client.post("/team/projects/", headers=ADMIN,
                                           json={"name": "broken", "permissions": ["fly"]})
        assert invalid_policy.status_code == 409 and "Invalid authority policy" in invalid_policy.text
        listing = await client.get("/team/projects/", headers=ADMIN)
        assert {row["name"] for row in listing.json()} >= {"docs", str(a), str(b)}
        bad = await client.post("/team/projects/members", headers=ADMIN, json={"principal_id": 20, "principal_name": "x"})
        assert bad.status_code == 422
        missing = await client.post("/team/projects/members", headers=ADMIN, json={"principal_name": "nobody"})
        assert missing.status_code == 404
        member = await client.post("/team/projects/members", headers=ADMIN,
                                   json={"principal_id": 20, "issuer": "corp-sso", "subject": "new@example.test"})
        assert member.status_code == 200 and member.json()["verified_at"] and not member.json()["is_admin"]
        preserved = await client.post("/team/projects/members", headers=ADMIN,
                                      json={"principal_id": 4, "issuer": "fixture", "subject": "4"})
        assert preserved.status_code == 200 and preserved.json()["is_admin"]
        demotion = await client.post("/team/projects/members", headers=ADMIN,
                                     json={"principal_id": 4, "issuer": "fixture", "subject": "4",
                                           "is_admin": False})
        assert demotion.status_code == 409
        members = await client.get("/team/projects/members", headers=ADMIN)
        assert {row["principal_id"] for row in members.json()} == {1, 2, 3, 4, 20}
        role = await client.put(f"/team/projects/{a}/members/20", headers=ADMIN, json={"role": "contributor"})
        assert role.status_code == 200 and role.json()["role"] == "contributor"
        assert (await client.put(f"/team/projects/{a}/members/20", headers=ADMIN, json={"role": "owner"})).status_code == 422
        assert (await client.put(f"/team/projects/{a}/members/99", headers=ADMIN, json={"role": "reader"})).status_code == 409
        roles = await client.get(f"/team/projects/{a}/members", headers=ADMIN)
        assert {row["principal_id"]: row["role"] for row in roles.json()}[20] == "contributor"
        attach = await client.post(f"/team/projects/{created.json()['id']}/repositories", headers=ADMIN,
                                   json={"repository_id": 1})
        assert attach.status_code == 409  # already attached to project a
        repos = await client.get(f"/team/projects/{a}/repositories", headers=ADMIN)
        assert [row["repository_id"] for row in repos.json()] == [1]
        gone = await client.delete(f"/team/projects/{a}/members/20", headers=ADMIN)
        assert gone.status_code == 200 and gone.json()["revoked_at"]
        assert (await client.delete("/team/projects/members/4", headers=ADMIN)).status_code == 409
        revoked = await client.delete("/team/projects/members/20", headers=ADMIN)
        assert revoked.status_code == 200 and revoked.json()["revoked_at"]
        assert (await client.delete("/team/projects/members/20", headers=ADMIN)).status_code == 200
    async with factory() as session:
        member = await session.get(TeamMember, 20)
        assert member.revoked_at is not None


@pytest.mark.asyncio
async def test_admin_api_resolves_numeric_principal_name_and_allows_slashless_redirect(team_db, monkeypatch):
    factory, _, _ = team_db
    async with factory() as session, session.begin():
        session.add(Principal(id=21, name="12345", kind="human"))
    app = team_app(factory, monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        redirect = await client.get("/team/projects", headers=ADMIN)
        assert redirect.status_code == 307 and redirect.headers["location"].endswith("/team/projects/")
        response = await client.post("/team/projects/members", headers=ADMIN,
                                     json={"principal_name": "12345", "issuer": "corp-sso",
                                           "subject": "numeric@example.test"})
        assert response.status_code == 200 and response.json()["principal_id"] == 21


@pytest.mark.asyncio
async def test_personal_or_run_credentials_cannot_administer(team_db, monkeypatch):
    factory, a, _ = team_db
    app = team_app(factory, monkeypatch)
    async with factory() as session, session.begin():
        session.add(Principal(id=30, name="run:fixture", kind="run"))
        await session.flush()
        session.add(ApiCredential(id=30, principal_id=30, key_hash=hash_credential("fixture-run"), scopes=["team:admin"]))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        assert (await client.get("/team/projects/", headers={"X-API-Key": "fixture-run"})).status_code == 403
        assert (await client.get(f"/team/projects/{a}/members", headers={"X-API-Key": "fixture-run"})).status_code == 403


def test_cli_provisions_end_to_end(team_db, monkeypatch):
    factory, a, _ = team_db
    monkeypatch.setattr(team_commands, "async_session_factory", factory)
    monkeypatch.setattr(team_commands, "init_db", AsyncMock())

    def invoke(*args):
        result = runner.invoke(cli, ["team", *args])
        return result

    init = invoke("init", "--name", "synthetic team")
    assert init.exit_code == 0, init.output
    assert json.loads(init.output) == {"id": 1, "name": "synthetic team"}
    assert team_commands.init_db.await_count == 1
    renamed = invoke("init", "--name", "other")
    assert renamed.exit_code == 1 and "already initialized" in renamed.output
    project = invoke("project", "add", "docs", "--permission", "read", "--permission", "task.create")
    assert project.exit_code == 0, project.output
    assert json.loads(project.output)["permissions"] == ["read", "task.create"]
    bad_project = invoke("project", "add", "broken", "--permission", "fly")
    assert bad_project.exit_code == 1 and "Invalid authority policy" in bad_project.output
    member = invoke("member", "add", "--principal", "operator", "--mint-key", "--admin",
                    "--project", "docs", "--role", "reviewer")
    assert member.exit_code == 0, member.output
    stdout, _, key_block = member.output.partition("New member credential")
    payload = json.loads(stdout)
    assert payload["is_admin"] and payload["verification_issuer"] == "brain-credential"
    assert payload["verification_subject"] == "operator" and payload["project_membership"]["role"] == "reviewer"
    raw_key = key_block.strip().splitlines()[-1]
    assert raw_key.startswith("pbk_")
    repeat = invoke("member", "add", "--principal", "operator", "--admin")
    assert repeat.exit_code == 0 and json.loads(repeat.output)["principal_id"] == payload["principal_id"]
    attach = invoke("repo", "attach", "--project", "docs", "--repository", "/synthetic/A")
    assert attach.exit_code == 1 and "another project" in attach.output
    attach = invoke("repo", "attach", "--project", str(a), "--repository", "/synthetic/A")
    assert attach.exit_code == 0 and json.loads(attach.output)["repository_id"] == 1
    listing = invoke("member", "list", "--project", "docs")
    assert [row["role"] for row in json.loads(listing.output)] == ["reviewer"]
    revoke = invoke("member", "revoke", "--principal", "operator", "--project", "docs")
    assert revoke.exit_code == 0 and json.loads(revoke.output)["revoked_at"]
    missing = invoke("member", "revoke", "--principal", "ghost")
    assert missing.exit_code == 1 and "not found" in missing.output

    async def minted_admin_can_use_api():
        app = team_app(factory, monkeypatch)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            response = await client.get("/team/projects/", headers={"X-API-Key": raw_key})
            assert response.status_code == 200, response.text
            assert "docs" in {row["name"] for row in response.json()}

    asyncio.run(minted_admin_can_use_api())


def test_cli_refuses_lite_storage(monkeypatch):
    monkeypatch.setattr(settings, "BRAIN_STORAGE_MODE", "lite")
    result = runner.invoke(cli, ["team", "project", "list"])
    assert result.exit_code == 1 and "BRAIN_STORAGE_MODE=full" in result.output
