import contextlib
import json

import httpx
import pytest
from fastapi import FastAPI
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from apps.api import auth, mcp_http
from apps.api.routers import team
from apps.api.team_boundary import TeamBoundary
from brain.config.settings import Settings, settings
from brain.workers.tasks import execute_job
from brain.workers.worker import run_worker
from brain.workers.scheduler import run_scheduler
from tests.test_mcp_http import _free_port, _serve
from tests.test_team_foundations import caller, create_task, task_payload, team_db as team_fixture

team_db = team_fixture


def team_app(factory, monkeypatch):
    monkeypatch.setattr(auth, "async_session_factory", factory)
    monkeypatch.setattr(team, "async_session_factory", factory)
    app = FastAPI()
    app.add_middleware(TeamBoundary)
    app.include_router(team.router)
    endpoint = mcp_http.install(app, public_url=None)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with endpoint.serving():
            yield

    app.router.lifespan_context = lifespan
    return app


@pytest.mark.asyncio
async def test_api_uses_live_authenticated_author_and_project_scope(team_db, monkeypatch):
    factory, a, b = team_db
    app = team_app(factory, monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        headers = {"X-API-Key": "fixture-2"}
        assert (await client.get(f"/team/projects/{a}/tasks")).status_code == 401
        assert (await client.get(f"/team/projects/{b}/tasks", headers=headers)).status_code == 403
        assert (await client.post(f"/team/projects/{a}/tasks", headers={"X-API-Key": "fixture-1"},
                                  json=task_payload())).status_code == 403
        forged = dict(task_payload(), actor="administrator", author_id=4)
        assert (await client.post(f"/team/projects/{a}/tasks", headers=headers, json=forged)).status_code == 422
        response = await client.post(f"/team/projects/{a}/tasks", headers=headers, json=task_payload())
        assert response.status_code == 200 and response.json()["author_id"] == 2
        read = await client.get(f"/team/projects/{a}/tasks", headers={"X-API-Key": "fixture-1"})
        assert read.status_code == 200 and len(read.json()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/harness/tasks", "/search", "/graph/nodes", "/rules", "/decisions",
                                  "/context-packs", "/web/repos", "/jobs", "/logs", "/artifacts",
                                  "/notifications", "/oauth/consent", "/admin", "/openapi.json"])
async def test_team_boundary_refuses_unadapted_surfaces_before_handlers(team_db, monkeypatch, path):
    factory, _, _ = team_db
    app = team_app(factory, monkeypatch)
    called = []

    @app.get(path)
    async def unsafe():
        called.append(True)
        return {"private": True}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.get(path, headers={"X-API-Key": "fixture-2"})
        assert response.status_code == 403 and not called


@pytest.mark.asyncio
async def test_native_mcp_only_project_tools_with_identical_denials(team_db, monkeypatch):
    factory, a, b = team_db
    async with factory() as session, session.begin():
        await create_task(session, caller(2), a, task_payload())
    app = team_app(factory, monkeypatch)
    with _serve(app, _free_port()) as url:
        async with httpx.AsyncClient(headers={"Authorization": "Bearer fixture-2"}, timeout=30) as http_client:
            async with Client(streamable_http_client(f"{url}/mcp", http_client=http_client)) as client:
                tools = await client.list_tools()
                assert {tool.name for tool in tools.tools} == {"team_list_tasks", "team_create_task", "team_request_run"}
                allowed = await client.call_tool("team_list_tasks", {"project_id": str(a)})
                assert not allowed.is_error, allowed.content
                assert json.loads(allowed.content[0].text)["author_id"] == 2
                denied = await client.call_tool("team_list_tasks", {"project_id": str(b)})
                assert denied.is_error
                assert (await client.call_tool("search_code", {"query": "private"})).is_error


@pytest.mark.asyncio
async def test_team_jobs_and_workers_refuse_before_setup(team_db):
    with pytest.raises(PermissionError, match="Unscoped"):
        await execute_job("reindex", {"repo_path": "/synthetic"})
    with pytest.raises(RuntimeError, match="not implemented"):
        await run_worker()
    with pytest.raises(RuntimeError, match="scheduling"):
        await run_scheduler(None, None)


def test_team_lite_rejected_and_personal_mode_remains_default():
    assert Settings(_env_file=None, BRAIN_TEAM_MODE=False, BRAIN_STORAGE_MODE="lite").BRAIN_STORAGE_MODE == "lite"
    with pytest.raises(ValueError, match="requires full"):
        Settings(_env_file=None, BRAIN_TEAM_MODE=True, BRAIN_STORAGE_MODE="lite")


@pytest.mark.asyncio
async def test_team_health_and_websockets_never_enter_personal_handlers(team_db, monkeypatch):
    called = []

    async def unsafe(*args):
        called.append(args)

    app = TeamBoundary(unsafe)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.get("/health")
        assert response.json() == {"status": "experimental", "mode": "team-foundations", "execution_available": False}
    messages = []

    async def send(message):
        messages.append(message)

    await app({"type": "websocket", "path": "/stream"}, None, send)
    assert messages == [{"type": "websocket.close", "code": 4403}] and called == []


@pytest.mark.asyncio
async def test_synthetic_or_shared_admin_cannot_access_team(team_db, monkeypatch):
    factory, a, _ = team_db
    monkeypatch.setattr(settings, "PROJECT_BRAIN_API_KEY", "fixture-legacy")
    app = team_app(factory, monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        result = await client.get(f"/team/projects/{a}/tasks", headers={"X-API-Key": "fixture-legacy"})
        assert result.status_code == 403
