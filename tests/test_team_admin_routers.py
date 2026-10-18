"""Global and administrative routers in the unified team model: a solo Brain lets every
authenticated caller through as before; once the team has a live member these routes need
a team administrator, while liveness probes stay readable by every member (trimmed) and
repository-scoped writes follow the project role (ADR docs/adr/team-collaboration.md)."""
import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI

from apps.api import auth, team_access
from apps.api.routers import (
    admin, app_settings, audit, autonomy, control, core_features, core_status, execution, routing, scheduler, setup,
    shadow,
)
from brain.config.settings import settings
from tests.test_team_foundations import team_db as team_fixture
from tests.test_team_transport import (
    ADMIN, CONTRIBUTOR, LEGACY, OUTSIDER, READER, add_outsider_and_unattached_repo, live_team_state,
    membership_driven, revoke_everyone,
)

team_db = team_fixture
pytestmark = pytest.mark.team_state

HEALTHY = {"postgres": {"status": "healthy"}, "redis": {"status": "healthy"}, "neo4j": {"status": "healthy"}}

# One representative request per administrative router (and per guarded route of the
# mixed ones), each stubbed so that an authorized caller gets HTTP 200.
ADMIN_ONLY = [
    ("GET", "/admin/principals", None),
    ("GET", "/api/settings", None),
    ("GET", "/audit/events", None),
    ("GET", "/autonomy/goals/synthetic", None),
    ("GET", "/control/summary", None),
    ("GET", "/control/queue", None),
    ("GET", "/control/health", None),
    ("GET", "/api/status/diagnostics", None),
    ("GET", "/api/status/indexing", None),
    ("GET", "/api/status/graph", None),
    ("GET", "/api/status/mcp/readiness", None),
    ("GET", "/api/status/recent-activity", None),
    ("GET", "/insights", None),
    ("GET", "/features", None),
    ("POST", "/execution/sessions?repo_id=r&task_category=general", None),
    ("GET", "/routing/summary", None),
    ("GET", "/shadow/summary", None),
    ("GET", "/scheduler/jobs", None),
    ("GET", "/api/setup/providers", None),
    ("GET", "/api/setup/status", None),
]
OPEN_TO_MEMBERS = [
    ("GET", "/api/status", None),
    ("GET", "/api/status/services", None),
    ("GET", "/api/status/mcp", None),
    ("GET", "/control/actions", None),
]


class _FakeGraphResult:
    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class _FakeGraphSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def run(self, _query):
        return _FakeGraphResult()


async def _stub_endpoint():
    return {"stubbed": True}


def admin_app(factory, monkeypatch):
    """Every router of this batch over the synthetic team database, with backends stubbed."""
    for module in (auth, team_access, admin):
        monkeypatch.setattr(module, "async_session_factory", factory)
    monkeypatch.setattr(team_access, "current_team_state", live_team_state)
    # core_status reads services, counts and helpers; none of it is under test here.
    monkeypatch.setattr(core_status, "check_health", AsyncMock(return_value=HEALTHY))
    monkeypatch.setattr(core_status, "get_db_counts", AsyncMock(return_value={"files": 3}))
    monkeypatch.setattr(core_status, "get_git_commit_hash", lambda _path: "c" * 40)
    monkeypatch.setattr(core_status, "async_session_factory", factory)
    monkeypatch.setattr(core_status, "get_self_diagnosis_status", AsyncMock(return_value={"status": "ok"}))
    monkeypatch.setattr(core_status, "get_mcp_runtime_status", AsyncMock(return_value={"available": True, "tools": []}))
    monkeypatch.setattr(core_status, "get_mcp_readiness_status", AsyncMock(return_value={"ready": True}))
    monkeypatch.setattr(core_status, "_tool_usage", AsyncMock(return_value=None))
    monkeypatch.setattr(core_status, "get_neo4j_counts", AsyncMock(return_value={}))
    monkeypatch.setattr(core_status, "get_top_connected_nodes", AsyncMock(return_value=([], None)))
    monkeypatch.setattr(core_status, "get_embedding_retrieval_health", AsyncMock(return_value={}))
    monkeypatch.setattr(core_status, "get_reports_list", lambda: [])
    for name in ("api_indexing_status", "api_recent_activity", "list_insights"):  # tables the fixture lacks
        route = next(r for r in core_status.router.routes if getattr(r, "name", "") == name)
        monkeypatch.setattr(route, "endpoint", _stub_endpoint)  # include_router below copies .endpoint
    monkeypatch.setattr("brain.database.session.neo4j_driver", SimpleNamespace(session=_FakeGraphSession))
    monkeypatch.setattr(core_features, "resolve_repo_path", lambda path: path)
    monkeypatch.setattr(core_features, "require_repository_by_path", AsyncMock(return_value=SimpleNamespace(id=1)))
    graph_client = MagicMock()
    graph_client.return_value.create_node = AsyncMock()
    monkeypatch.setattr(core_features, "GraphClient", graph_client)
    plane = MagicMock()
    plane.summary.return_value = plane.health.return_value = {}
    plane.action_queue.return_value = []
    monkeypatch.setattr(control, "_plane", lambda _registry=None: plane)
    monkeypatch.setattr(control, "workspace_registry", AsyncMock(return_value=None))
    monkeypatch.setattr(audit, "_fetch", AsyncMock(return_value=[]))
    monkeypatch.setattr(autonomy._goal_service, "get_goal_status", lambda _goal: SimpleNamespace(
        goal_id="synthetic", description="", current_phase=SimpleNamespace(value="GOAL_RECEIVED"),
        commit_sha=None, deployed=False, worktree_path=None))
    monkeypatch.setattr(scheduler, "scheduler_status",
                        AsyncMock(return_value={"enabled": True, "grace_seconds": 60, "stale": [], "jobs": []}))
    queue = AsyncMock()
    queue.enqueue = AsyncMock(return_value="job-1")
    monkeypatch.setattr(scheduler, "queue_for_job", lambda *_a, **_k: queue)
    monkeypatch.setattr(setup, "collect_setup_status", AsyncMock(return_value={"steps": []}))
    monkeypatch.setattr("apps.api.request_observation.observe_http_request", AsyncMock())
    app = FastAPI()
    for module in (admin, app_settings, audit, autonomy, control, core_features, core_status, execution, routing,
                   scheduler, setup, shadow):
        app.include_router(module.router)
    return app


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test")


async def call(client, method, path, headers=None, body=None):
    return await client.request(method, path, headers=headers or {}, json=body)


def signed(payload: dict, secret: str = "s3cret") -> tuple[bytes, dict]:
    raw = json.dumps(payload).encode()
    digest = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {"X-Hub-Signature-256": f"sha256={digest}", "Content-Type": "application/json"}


def feature(repo_path):
    return {"name": "synthetic", "properties": {}, "repo_path": repo_path}


@pytest.fixture
def stubbed(team_db, monkeypatch):
    factory, _, _ = team_db
    membership_driven(monkeypatch)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_API_KEY", "fixture-legacy")
    monkeypatch.setattr(settings, "PROJECT_BRAIN_WEBHOOK_TOKEN", "s3cret")
    monkeypatch.setattr(settings, "PROJECT_BRAIN_WEBHOOK_REF", "refs/heads/main")
    return factory, admin_app(factory, monkeypatch)


@pytest.mark.asyncio
async def test_solo_admin_routes_unchanged_for_shared_key_and_members(stubbed):
    factory, app = stubbed
    await revoke_everyone(factory)
    async with client_for(app) as client:
        for headers in (LEGACY, READER):
            for method, path, body in ADMIN_ONLY + OPEN_TO_MEMBERS:
                response = await call(client, method, path, headers, body)
                assert response.status_code == 200, (path, headers, response.text)
            full = (await client.get("/api/status", headers=headers)).json()
            assert {"repo_path", "commit_hash", "counts"} <= full.keys()
            assert (await client.post("/features", json=feature("/synthetic/A"), headers=headers)).status_code == 200
            webhook = await client.post("/webhooks/git-merge", headers=headers,
                                        json={"ref": "refs/heads/main", "sha": "abc", "repo_path": "/synthetic/C"})
            assert webhook.status_code == 200 and webhook.json()["status"] == "queued"


@pytest.mark.asyncio
async def test_team_state_admin_routes_need_a_team_administrator(stubbed):
    factory, app = stubbed
    await add_outsider_and_unattached_repo(factory)
    async with client_for(app) as client:
        for method, path, body in ADMIN_ONLY:
            for headers in (READER, CONTRIBUTOR, OUTSIDER):
                denied = await call(client, method, path, headers, body)
                assert denied.status_code == 403, (path, headers, denied.text)
                if path != "/insights":  # repository_access words its own denial
                    assert "team administrator required" in denied.json()["detail"].lower()
            allowed = await call(client, method, path, ADMIN, body)
            assert allowed.status_code == 200, (path, allowed.text)
        # Mutations behind the router-level guard are refused before the handler runs.
        assert (await client.patch("/api/settings", json={}, headers=CONTRIBUTOR)).status_code == 403
        assert (await client.post("/admin/credentials/1/revoke", headers=CONTRIBUTOR)).status_code == 403
        assert (await client.post("/shadow/executions/x/cancel", headers=READER)).status_code == 403


@pytest.mark.asyncio
async def test_team_state_refuses_shared_key_and_dev_open_on_admin_routes(stubbed, monkeypatch):
    _, app = stubbed
    async with client_for(app) as client:
        for method, path, body in ADMIN_ONLY + OPEN_TO_MEMBERS:
            denied = await call(client, method, path, LEGACY, body)
            assert denied.status_code == 403, (path, denied.text)
            assert "named credential" in denied.json()["detail"].lower()
        webhook = await client.post("/webhooks/git-merge", headers=LEGACY,
                                    json={"ref": "refs/heads/main", "repo_path": "/synthetic/A"})
        assert webhook.status_code == 401
    monkeypatch.setattr(settings, "PROJECT_BRAIN_API_KEY", "")
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    async with client_for(app) as client:
        for method, path, body in ADMIN_ONLY + OPEN_TO_MEMBERS:
            keyless = await call(client, method, path, None, body)
            assert keyless.status_code == 401, (path, keyless.text)
            assert "named credential" in keyless.json()["detail"].lower()


@pytest.mark.asyncio
async def test_team_state_liveness_stays_open_to_members_but_trimmed(stubbed):
    _, app = stubbed
    async with client_for(app) as client:
        for method, path, body in OPEN_TO_MEMBERS:
            for headers in (READER, CONTRIBUTOR, ADMIN):
                response = await call(client, method, path, headers, body)
                assert response.status_code == 200, (path, headers, response.text)
        member = (await client.get("/api/status", headers=READER)).json()
        assert member == {"status": "ok", "version": member["version"], "services": HEALTHY}
        administrator = (await client.get("/api/status", headers=ADMIN)).json()
        assert administrator["commit_hash"] == "c" * 40 and administrator["counts"] == {"files": 3}
        assert "repo_path" in administrator


@pytest.mark.asyncio
async def test_team_state_repository_scoped_routes_follow_project_roles(stubbed):
    _, app = stubbed
    async with client_for(app) as client:
        # Insights of one repository: project read (the list is empty until insights get an owner).
        assert (await client.get("/insights", params={"repository_id": 1}, headers=READER)).status_code == 200
        assert (await client.get("/insights", params={"repository_id": 2}, headers=READER)).status_code == 403
        # Writing a Feature into one repository's graph: a contributor on its project.
        assert (await client.post("/features", json=feature("/synthetic/A"), headers=CONTRIBUTOR)).status_code == 200
        assert (await client.post("/features", json=feature("/synthetic/A"), headers=READER)).status_code == 403
        assert (await client.post("/features", json=feature("/synthetic/B"), headers=CONTRIBUTOR)).status_code == 403
        # The API-key webhook path requests a reindex like POST /jobs/reindex does.
        for headers, repo_path, expected in ((CONTRIBUTOR, "/synthetic/A", 200), (READER, "/synthetic/A", 403),
                                             (CONTRIBUTOR, "/not/indexed", 403), (ADMIN, "/not/indexed", 200)):
            response = await client.post("/webhooks/git-merge", headers=headers,
                                         json={"ref": "refs/heads/main", "sha": "abc", "repo_path": repo_path})
            assert response.status_code == expected, (headers, repo_path, response.text)
        # The operator's signed webhook needs no member at all.
        raw, headers = signed({"ref": "refs/heads/main", "sha": "abc", "repo_path": "/not/indexed"})
        response = await client.post("/webhooks/git-merge", content=raw, headers=headers)
        assert response.status_code == 200 and response.json()["status"] == "queued"
