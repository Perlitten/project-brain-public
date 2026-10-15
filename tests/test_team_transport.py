"""Unified team model over HTTP and native MCP: solo is a team of one, boundaries
switch on with the first live team member (ADR docs/adr/team-collaboration.md)."""
import asyncio
import contextlib
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import update

from apps.api import auth, mcp_http, team_access
from apps.api.routers import core_memory, core_retrieval, jobs, team
from brain.auth.principals import hash_credential
from brain.config.settings import Settings, settings
from brain.database.models import ApiCredential, Principal, Repository
from brain.team.authorization import team_state
from brain.team.models import TeamMember
from brain.workers.scheduler import run_scheduler
from tests.test_mcp_http import _free_port, _serve
from tests.test_team_foundations import caller, create_task, task_payload, team_db as team_fixture

team_db = team_fixture
pytestmark = pytest.mark.team_state

LEGACY = {"X-API-Key": "fixture-legacy"}
READER, CONTRIBUTOR, ADMIN, OUTSIDER = ({"X-API-Key": f"fixture-{i}"} for i in (1, 2, 4, 5))


def membership_driven(monkeypatch):
    """Team state comes from the members table, not from the deprecated strict flag."""
    monkeypatch.setattr(settings, "BRAIN_TEAM_MODE", False)


async def revoke_everyone(factory):
    async with factory() as session, session.begin():
        await session.execute(update(TeamMember).values(revoked_at=datetime.now(timezone.utc)))


async def add_outsider_and_unattached_repo(factory):
    """Principal 5 has a key but no membership; repository 3 belongs to no project."""
    async with factory() as session, session.begin():
        session.add(Principal(id=5, name="human-5", kind="human"))
        await session.flush()
        session.add(ApiCredential(id=5, principal_id=5, key_hash=hash_credential("fixture-5"), scopes=["*"]))
        session.add(Repository(id=3, name="C", path="/synthetic/C"))


async def live_team_state() -> str:
    """The real probe against the test database (conftest stubs it to "solo" for DB-less tests)."""
    async with team_access.async_session_factory() as session:
        return await team_state(session)


def personal_app(factory, monkeypatch):
    """Team routes plus the personal surfaces this PR routes through authorize()."""
    for module in (auth, team, team_access):
        monkeypatch.setattr(module, "async_session_factory", factory)
    monkeypatch.setattr(team_access, "current_team_state", live_team_state)
    monkeypatch.setattr(core_memory.LearningStore, "list_active_learnings", AsyncMock(return_value=[]))
    monkeypatch.setattr(core_memory.DecisionStore, "add_decision", AsyncMock(return_value=7))
    monkeypatch.setattr(core_retrieval, "hybrid_search_code", AsyncMock(return_value=[]))
    monkeypatch.setattr("apps.api.request_observation.observe_http_request", AsyncMock())
    queue = AsyncMock()
    queue.enqueue = AsyncMock(return_value="job-1")
    monkeypatch.setattr(jobs, "_queue", lambda *_: queue)
    app = FastAPI()
    for router in (team.router, core_memory.router, core_retrieval.router, jobs.router):
        app.include_router(router)
    endpoint = mcp_http.install(app, public_url=None)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with endpoint.serving():
            yield

    app.router.lifespan_context = lifespan
    return app


team_app = personal_app  # name the provisioning tests use


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test")


def decision(repo_path):
    return {"title": "synthetic", "description": "no provider calls", "repo_path": repo_path}


@pytest.mark.asyncio
async def test_team_state_follows_live_membership_and_strict_flag(team_db, monkeypatch):
    factory, _, _ = team_db
    membership_driven(monkeypatch)
    monkeypatch.setattr(team_access, "async_session_factory", factory)
    async with factory() as session:
        assert await team_state(session) == "team"
    assert await team_access.current_team_state() == "team"
    await revoke_everyone(factory)
    assert await team_access.current_team_state() == "solo"
    monkeypatch.setattr(settings, "BRAIN_TEAM_MODE", True)  # deprecated strictness alias
    assert await team_access.current_team_state() == "team"
    monkeypatch.setattr(settings, "BRAIN_STORAGE_MODE", "lite")
    monkeypatch.setattr(settings, "BRAIN_TEAM_MODE", False)
    assert await team_access.current_team_state() == "solo"


@pytest.mark.asyncio
async def test_solo_personal_routes_work_with_shared_key_and_dev_open(team_db, monkeypatch):
    factory, a, _ = team_db
    membership_driven(monkeypatch)
    await revoke_everyone(factory)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_API_KEY", "fixture-legacy")
    app = personal_app(factory, monkeypatch)
    async with client_for(app) as client:
        for headers in (LEGACY, READER):
            assert (await client.get("/learnings", params={"repo_path": "/synthetic/A"}, headers=headers)).status_code == 200
            assert (await client.get("/learnings", headers=headers)).status_code == 200
            assert (await client.post("/search", json={"query": "q", "repo_path": "/synthetic/A"}, headers=headers)).status_code == 200
            assert (await client.post("/search", json={"query": "q", "repo_path": "/not/indexed"}, headers=headers)).status_code == 200
            assert (await client.post("/decisions", json=decision("/synthetic/A"), headers=headers)).status_code == 200
            assert (await client.post("/decisions", json=decision(None), headers=headers)).status_code == 200
            assert (await client.post("/jobs/reindex", json={"repo_path": "/synthetic/A"}, headers=headers)).status_code == 200
        # The team surface itself still needs a team administrator: a team of one has none yet.
        assert (await client.get(f"/team/projects/{a}/tasks", headers=LEGACY)).status_code == 403
    monkeypatch.setattr(settings, "PROJECT_BRAIN_API_KEY", "")
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    async with client_for(app) as client:
        assert (await client.get("/learnings", params={"repo_path": "/synthetic/A"})).status_code == 200


@pytest.mark.asyncio
async def test_team_state_outsider_denied_reader_reads_contributor_writes(team_db, monkeypatch):
    factory, a, _ = team_db
    membership_driven(monkeypatch)
    await add_outsider_and_unattached_repo(factory)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_API_KEY", "fixture-legacy")
    app = personal_app(factory, monkeypatch)
    async with client_for(app) as client:
        for headers in (OUTSIDER, LEGACY):
            for path in ("/synthetic/A", "/synthetic/C"):
                assert (await client.get("/learnings", params={"repo_path": path}, headers=headers)).status_code == 403
                assert (await client.post("/search", json={"query": "q", "repo_path": path}, headers=headers)).status_code == 403
                assert (await client.post("/decisions", json=decision(path), headers=headers)).status_code == 403
        denied = await client.get("/learnings", params={"repo_path": "/synthetic/A"}, headers=LEGACY)
        assert "named credential" in denied.json()["detail"].lower()
        # Reader: reads the attached repository, cannot propose knowledge or request runs.
        assert (await client.get("/learnings", params={"repo_path": "/synthetic/A"}, headers=READER)).status_code == 200
        assert (await client.post("/search", json={"query": "q", "repo_path": "/synthetic/A"}, headers=READER)).status_code == 200
        assert (await client.post("/decisions", json=decision("/synthetic/A"), headers=READER)).status_code == 403
        assert (await client.post("/jobs/reindex", json={"repo_path": "/synthetic/A"}, headers=READER)).status_code == 403
        # Contributor: proposes and requests runs on the attached repository only.
        assert (await client.post("/decisions", json=decision("/synthetic/A"), headers=CONTRIBUTOR)).status_code == 200
        assert (await client.post("/jobs/reindex", json={"repo_path": "/synthetic/A"}, headers=CONTRIBUTOR)).status_code == 200
        assert (await client.post("/decisions", json=decision("/synthetic/B"), headers=CONTRIBUTOR)).status_code == 403
        # Unattached, unindexed and unscoped surfaces need a team administrator.
        for headers, expected in ((READER, 403), (CONTRIBUTOR, 403), (ADMIN, 200)):
            assert (await client.get("/learnings", params={"repo_path": "/synthetic/C"}, headers=headers)).status_code == expected
            assert (await client.get("/learnings", headers=headers)).status_code == expected
            assert (await client.post("/search", json={"query": "q", "repo_path": "/not/indexed"}, headers=headers)).status_code == expected
            assert (await client.post("/decisions", json=decision(None), headers=headers)).status_code == expected
            assert (await client.post("/jobs/reindex", headers=headers)).status_code == expected
        assert (await client.get(f"/team/projects/{a}/tasks", headers=OUTSIDER)).status_code == 403
    monkeypatch.setattr(settings, "PROJECT_BRAIN_API_KEY", "")
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    async with client_for(app) as client:
        keyless = await client.get("/learnings", params={"repo_path": "/synthetic/A"})
        assert keyless.status_code == 401 and "named credential" in keyless.json()["detail"].lower()


@pytest.mark.asyncio
async def test_api_uses_live_authenticated_author_and_project_scope(team_db, monkeypatch):
    factory, a, b = team_db
    membership_driven(monkeypatch)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    app = personal_app(factory, monkeypatch)
    async with client_for(app) as client:
        anonymous = await client.get(f"/team/projects/{a}/tasks")
        assert anonymous.status_code == 401
        assert "named credential" in anonymous.json()["detail"].lower()
        assert (await client.get(f"/team/projects/{b}/tasks", headers=CONTRIBUTOR)).status_code == 403
        assert (await client.post(f"/team/projects/{a}/tasks", headers=READER, json=task_payload())).status_code == 403
        forged = dict(task_payload(), actor="administrator", author_id=4)
        assert (await client.post(f"/team/projects/{a}/tasks", headers=CONTRIBUTOR, json=forged)).status_code == 422
        response = await client.post(f"/team/projects/{a}/tasks", headers=CONTRIBUTOR, json=task_payload())
        assert response.status_code == 200 and response.json()["author_id"] == 2
        read = await client.get(f"/team/projects/{a}/tasks", headers=READER)
        assert read.status_code == 200 and len(read.json()) == 1


@pytest.mark.asyncio
async def test_native_mcp_serves_personal_and_team_tools_with_identical_denials(team_db, monkeypatch):
    factory, a, b = team_db
    membership_driven(monkeypatch)
    async with factory() as session, session.begin():
        await create_task(session, caller(2), a, task_payload())
    app = personal_app(factory, monkeypatch)
    with _serve(app, _free_port()) as url:
        async with httpx.AsyncClient(headers={"Authorization": "Bearer fixture-2"}, timeout=30) as http_client:
            async with Client(streamable_http_client(f"{url}/mcp", http_client=http_client)) as client:
                names = {tool.name for tool in (await client.list_tools()).tools}
                assert {"team_list_tasks", "team_create_task", "team_request_run", "search_code"} <= names
                allowed = await client.call_tool("team_list_tasks", {"project_id": str(a)})
                assert not allowed.is_error, allowed.content
                assert json.loads(allowed.content[0].text)["author_id"] == 2
                assert (await client.call_tool("team_list_tasks", {"project_id": str(b)})).is_error


@pytest.mark.asyncio
async def test_native_mcp_refuses_shared_key_only_once_the_team_has_members(team_db, monkeypatch):
    factory, _, _ = team_db
    membership_driven(monkeypatch)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_API_KEY", "fixture-legacy")
    app = personal_app(factory, monkeypatch)
    with _serve(app, _free_port()) as url:
        async with httpx.AsyncClient(headers={"Authorization": "Bearer fixture-legacy"}, timeout=30) as http_client:
            assert (await http_client.post(f"{url}/mcp", json={})).status_code == 401
            await revoke_everyone(factory)
            async with Client(streamable_http_client(f"{url}/mcp", http_client=http_client)) as client:
                assert "search_code" in {tool.name for tool in (await client.list_tools()).tools}


@pytest.mark.asyncio
async def test_lifespan_and_scheduler_no_longer_refuse_a_team(monkeypatch):
    from apps.api import main

    for name, value in {"BRAIN_TEAM_MODE": True, "LATE_INTERACTION_ENABLED": True,
                        "LATE_INTERACTION_SHADOW_ENABLED": True, "LATE_INTERACTION_REMOTE_ENABLED": False,
                        "SCHEDULER_ENABLED": False}.items():
        monkeypatch.setattr(settings, name, value)
    for name in ("init_db", "close_database_connections", "close_late_interaction_client",
                 "close_lfm_colbert_provider"):
        monkeypatch.setattr(main, name, AsyncMock())
    monkeypatch.setattr(main, "_mcp_endpoint", None)
    warmup = AsyncMock()
    monkeypatch.setattr(main, "warmup_lfm_colbert_provider", warmup)
    async with main.lifespan(main.app):
        await asyncio.sleep(0)
    warmup.assert_called_once()
    assert await run_scheduler(None, asyncio.Event()) is None


def test_team_lite_rejected_and_personal_mode_remains_default():
    assert Settings(_env_file=None, BRAIN_TEAM_MODE=False, BRAIN_STORAGE_MODE="lite").BRAIN_STORAGE_MODE == "lite"
    with pytest.raises(ValueError, match="requires full"):
        Settings(_env_file=None, BRAIN_TEAM_MODE=True, BRAIN_STORAGE_MODE="lite")


@pytest.mark.asyncio
async def test_synthetic_or_shared_admin_cannot_access_team(team_db, monkeypatch):
    factory, a, _ = team_db
    monkeypatch.setattr(settings, "PROJECT_BRAIN_API_KEY", "fixture-legacy")
    app = personal_app(factory, monkeypatch)
    async with client_for(app) as client:
        result = await client.get(f"/team/projects/{a}/tasks", headers=LEGACY)
        assert result.status_code == 403
