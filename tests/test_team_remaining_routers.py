"""Team-state authorization on the routes the earlier team PRs left unscoped: the harness task
surfaces, job status, core memory, late-interaction status, web reports and health, impact runs,
graph/drift/remediation writes, web-login configuration and the Telegram bridge. Solo state stays
a team of one."""
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from apps.api import auth, team_access
from apps.api.routers import (
    core_lifecycle,
    core_memory,
    core_retrieval,
    drift,
    graph_v2,
    harness,
    impact,
    jobs,
    remediation,
    telegram_bridge,
    web,
    web_login,
)
from brain.config.settings import settings
from tests.test_team_foundations import team_db as team_fixture
from tests.test_team_transport import live_team_state, membership_driven, revoke_everyone

team_db = team_fixture
pytestmark = pytest.mark.team_state

LEGACY = {"X-API-Key": "fixture-legacy"}
READER, CONTRIBUTOR, REVIEWER, ADMIN = ({"X-API-Key": f"fixture-{i}"} for i in (1, 2, 3, 4))
DENIED = "Repository access denied"
ADMIN_REQUIRED = "Team administrator required"
NAMED = "Named credential required"
REACHED = 418  # sentinel status: the handler ran, so authorization let the call through

# Harness tasks on project A's and B's repositories, and one on a path no project owns.
TASK_A, TASK_B, TASK_Z = (str(uuid.uuid4()) for _ in range(3))
TASK_REPOS = {TASK_A: "/synthetic/A", TASK_B: "/synthetic/B", TASK_Z: "/synthetic/unattached"}
# Queued jobs: one per repository (by path and by id) and one without a repository.
JOBS = {
    "job-a": {"id": "job-a", "status": "completed", "params": {"repo_path": "/synthetic/A"}},
    "job-b": {"id": "job-b", "status": "completed", "params": {"repository_id": 2}},
    "job-global": {"id": "job-global", "status": "completed", "params": None},
}


def _reached(*_args, **_kwargs):
    raise HTTPException(status_code=REACHED, detail="handler reached")


def _fake_harness():
    async def get_task(tid):
        repo = TASK_REPOS.get(str(tid))
        return SimpleNamespace(id=tid, repo_path=repo) if repo else None

    row = AsyncMock(return_value=SimpleNamespace())
    rows = AsyncMock(return_value=[])
    return SimpleNamespace(
        get_task=get_task, create_task=row, add_artifact=row, acquire_lease=row, add_handoff=row,
        add_acceptance_checkpoint=row, list_tasks=rows, list_artifacts=rows, list_validations=rows,
        list_handoffs=rows, count_tasks=AsyncMock(return_value=0), task_status_counts=AsyncMock(return_value={}),
    )


def remaining_app(factory, monkeypatch, tmp_path):
    """Every touched router over the fixture database, with storage and engines stubbed out."""
    monkeypatch.chdir(tmp_path)
    for module in (auth, team_access, telegram_bridge, core_memory):
        monkeypatch.setattr(module, "async_session_factory", factory)
    monkeypatch.setattr(team_access, "current_team_state", live_team_state)
    monkeypatch.setattr("apps.api.request_observation.observe_http_request", AsyncMock())
    # harness
    fake_harness = _fake_harness()
    monkeypatch.setattr(harness, "HarnessStore", fake_harness)
    monkeypatch.setattr("brain.memory.harness_store.HarnessStore.get_task", fake_harness.get_task)
    for name in ("_task_dict", "_artifact_dict", "_lease_dict", "_handoff_dict", "_event_dict"):
        monkeypatch.setattr(harness, name, lambda _row: {"ok": True})
    # jobs
    monkeypatch.setattr(settings, "BRAIN_WORKER_POOLS_V2_ENABLED", False)
    queue = SimpleNamespace(prefix="brain:jobs",
                            get_job=AsyncMock(side_effect=lambda job_id: dict(JOBS[job_id]) if job_id in JOBS else None))
    monkeypatch.setattr(jobs, "_queue", lambda *_: queue)
    # core memory
    monkeypatch.setattr(core_memory.DecisionStore, "list_decisions", AsyncMock(return_value=[]))
    monkeypatch.setattr(core_memory.RuleStore, "list_rules", AsyncMock(return_value=[]))
    monkeypatch.setattr(core_memory.LearningStore, "add_learning", AsyncMock(return_value=7))
    monkeypatch.setattr(core_memory.LearningStore, "reject", AsyncMock())
    monkeypatch.setattr("brain.memory.skill_store.list_skills", AsyncMock(return_value=[]))
    monkeypatch.setattr("brain.memory.skill_store.create_skill", AsyncMock(side_effect=_reached))
    monkeypatch.setattr("brain.memory.skill_store.decide_skill", AsyncMock(side_effect=LookupError("no skill")))
    monkeypatch.setattr("brain.memory.consolidation.approve_episode", AsyncMock(side_effect=LookupError("none")))
    monkeypatch.setattr("brain.memory.consolidation.reject_episode", AsyncMock(side_effect=LookupError("none")))
    monkeypatch.setattr("brain.memory.skill_outcomes.record_skill_outcome", AsyncMock(side_effect=LookupError("x")))
    monkeypatch.setattr("brain.llm.get_embedding_provider", lambda: SimpleNamespace(embed=AsyncMock(return_value=[])))
    # late interaction
    monkeypatch.setattr(core_lifecycle, "resolve_repo_path", lambda path: path)
    monkeypatch.setattr(core_lifecycle, "require_repository_by_path", AsyncMock(return_value=SimpleNamespace()))
    monkeypatch.setattr(core_lifecycle, "collect_late_interaction_operator_status",
                        AsyncMock(return_value={"status": "ok"}))
    # web
    monkeypatch.setattr(web, "get_reports_list", lambda: [])
    monkeypatch.setattr(web, "reports_dir", lambda *_: tmp_path)
    monkeypatch.setattr(web, "check_health", AsyncMock(side_effect=_reached))
    # impact: two cached runs bound to the repositories they analyzed
    for run_id, repository_id in (("run-a", 1), ("run-b", 2)):
        monkeypatch.setitem(impact._IMPACT_CACHE, run_id, SimpleNamespace(
            to_dict=lambda: {"ok": True}, suggested_tests=[], impacted_entities=[]))
        monkeypatch.setitem(impact._IMPACT_REPOSITORY_ID, run_id, repository_id)
    # graph, drift, remediation: the handler's first step proves authorization passed
    for module in (graph_v2, drift, remediation):
        monkeypatch.setattr(module, "_safe_resolve", _reached)
    # web-login configuration writes
    monkeypatch.setattr(web_login, "_write", lambda values: None)
    # telegram bridge: project slug names the repository; ask_project answers from a stub once authorized
    monkeypatch.setattr(telegram_bridge, "_resolve_repo", AsyncMock(
        side_effect=lambda question, slug: ({"A": "/synthetic/A", "B": "/synthetic/B"}[slug], question)))
    monkeypatch.setattr(core_retrieval, "_agent_v2_mode", lambda: "on")
    monkeypatch.setattr(core_retrieval, "_ask_v2", AsyncMock(return_value={"answer": "fine"}))
    app = FastAPI()
    for module in (harness, jobs, core_memory, core_lifecycle, web, impact, graph_v2, drift, remediation,
                   web_login, telegram_bridge):
        app.include_router(module.router)
    return app


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test")


def task_body(repo_path):
    return {"title": "t", "goal": "g", "repo_path": repo_path}


ARTIFACT = {"kind": "log", "path_or_uri": "file:///synthetic.log"}
LEASE = {"worker_name": "w"}
CHECKPOINT = {
    "attempt_id": "a1", "external_task_id": "x1", "authority": "human", "idempotency_key": "idem-0001",
    "expected_task_version": 0, "contract_sha256": "0" * 64, "gate_report_sha256": "0" * 64,
    "ledger_sha256": "0" * 64, "brain_context_sha256": "0" * 64, "result_commit": "0" * 40,
    "repository_fingerprint": "0" * 64, "fencing_token": 1,
}

# Reads scoped by the task, run or job they name, or by the repository in the query string.
# Each is (method, path for repository A or B, query params, json body).
def scoped_reads(repo):
    task, run, job = {"A": (TASK_A, "run-a", "job-a"), "B": (TASK_B, "run-b", "job-b")}[repo]
    path = f"/synthetic/{repo}"
    return (
        ("GET", "/harness/tasks", {"repo_path": path}, None),
        ("GET", f"/harness/tasks/{task}", {}, None),
        ("GET", f"/harness/tasks/{task}/artifacts", {}, None),
        ("GET", "/harness/handoffs", {"task_id": task}, None),
        ("GET", f"/jobs/{job}", {}, None),
        ("GET", "/late-interaction/status", {"repo_path": path}, None),
        # The impact router reads its scope from the query string; the run is checked against its own repository.
        ("GET", f"/insights/impact/{run}", {"repo_path": path}, None),
        ("GET", f"/insights/impact/{run}/tests", {"repo_path": path}, None),
    )


def outcome_body(task_id):
    return {"task_id": task_id, "validation_id": 1, "outcome": "success",
            "evidence": {"artifact_ids": [1], "source_refs": ["r"]}}


# Writes that need run.request or knowledge.propose (contributor) on the repository.
def run_writes(repo):
    task = {"A": TASK_A, "B": TASK_B}[repo]
    path = f"/synthetic/{repo}"
    return (
        ("POST", "/harness/tasks", {}, task_body(path)),
        ("POST", f"/harness/tasks/{task}/artifacts", {}, ARTIFACT),
        ("POST", f"/harness/tasks/{task}/leases", {}, LEASE),
        ("POST", "/harness/handoffs", {}, {"session_id": "s", "summary": "x", "task_id": task}),
        ("POST", "/insights/graph/build", {"repo_path": path}, None),
        ("POST", "/insights/graph/activate/gen-1", {"repo_path": path}, None),
        ("POST", "/insights/graph/rollback", {"repo_path": path}, None),
        ("POST", "/insights/architectural-drift/scan", {"repo_path": path}, None),
        ("POST", "/insights/architectural-drift/reviews/r1/acknowledge", {"repo_path": path}, None),
        ("POST", "/insights/remediation/plans", {"repo_path": path, "file_path": "x.py"}, None),
        ("POST", "/insights/remediation/plans/p1/acknowledge", {"repo_path": path}, None),
        ("POST", "/insights/remediation/plans/p1/mark-implemented", {"repo_path": path}, None),
        ("POST", "/insights/remediation/plans/p1/verify", {"repo_path": path}, None),
        ("POST", "/learnings", {}, {"statement": "s", "repo_scope": path}),
        ("POST", "/skills", {}, {"name": "n", "description": "d", "repo_scope": path}),
        ("POST", "/skills/1/outcomes", {}, outcome_body(task)),
    )


# Decisions that need task.accept (reviewer) on the repository.
def accept_writes(repo):
    task = {"A": TASK_A, "B": TASK_B, "Z": TASK_Z}[repo]
    path = f"/synthetic/{repo}"
    return (
        ("POST", f"/harness/tasks/{task}/acceptance-checkpoints", {}, CHECKPOINT),
        ("POST", "/insights/architectural-drift/reviews/r1/approve", {"repo_path": path}, None),
        ("POST", "/insights/architectural-drift/reviews/r1/reject", {"repo_path": path}, None),
        ("POST", "/insights/remediation/plans/p1/approve", {"repo_path": path}, None),
        ("POST", "/insights/remediation/plans/p1/reject", {"repo_path": path}, None),
    )


# Normative memory listings: every member, trimmed to global rows and readable repositories.
MEMBER_FILTERED = (
    ("GET", "/decisions", None),
    ("GET", "/rules", None),
)

# Brain-wide surfaces: a team administrator once the team has members.
ADMIN_ONLY = (
    ("GET", "/episodes", None),
    ("POST", "/episodes/search", {"query": "q"}),
    ("POST", "/episodes/1/approve", None),
    ("POST", "/episodes/1/reject", None),
    ("GET", "/skills", None),
    ("POST", "/skills/1/approve", None),
    ("POST", "/skills/1/reject", None),
    ("DELETE", "/learnings/1", None),
    ("GET", "/api/web/reports", None),
    ("GET", "/api/web/reports/report.md", None),
    ("GET", "/api/web/health", None),
    ("PUT", "/api/web-login", {"user": "owner", "password": "synthetic-pass"}),
    ("POST", "/api/web-login/disable", {"current_password": "synthetic-pass"}),
)


async def _call(client, method, path, params, body, headers):
    return await client.request(method, path, params=params, json=body, headers=headers)


def _authorized(response):
    return response.status_code not in (401, 403) and DENIED not in response.text and NAMED not in response.text


@pytest.mark.asyncio
async def test_team_member_reads_own_repository_and_not_the_other_project(team_db, monkeypatch, tmp_path):
    factory, _, _ = team_db
    membership_driven(monkeypatch)
    app = remaining_app(factory, monkeypatch, tmp_path)
    async with client_for(app) as client:
        for (method, path, params, body), (_, other_path, other_params, _) in zip(scoped_reads("A"),
                                                                                    scoped_reads("B")):
            own = await _call(client, method, path, params, body, READER)
            assert own.status_code == 200, (path, own.text)
            other = await _call(client, method, other_path, other_params, None, READER)
            # Objects bound to another repository answer like missing ones; query-scoped routes say why.
            assert other.status_code in (403, 404), (other_path, other.text)
        assert (await client.get(f"/harness/tasks/{TASK_B}", headers=READER)).status_code == 403
        assert (await client.get("/jobs/job-b", headers=READER)).json()["detail"] == "Job not found"
        assert (await client.get("/insights/impact/run-b", params={"repo_path": "/synthetic/A"},
                                 headers=READER)).status_code == 404
        # The administrator, contributor on B, reads B's objects; a job or task listing without a repository
        # is administrator-only.
        assert (await client.get("/jobs/job-b", headers=ADMIN)).status_code == 200
        assert (await client.get(f"/harness/tasks/{TASK_B}", headers=ADMIN)).status_code == 200
        for path in ("/jobs/job-global", "/harness/tasks", "/harness/handoffs"):
            refused = await client.get(path, headers=READER)
            assert refused.status_code in (403, 404), (path, refused.text)
            assert (await client.get(path, headers=ADMIN)).status_code == 200, path


@pytest.mark.asyncio
async def test_team_writes_need_run_request_and_decisions_need_accept(team_db, monkeypatch, tmp_path):
    factory, _, _ = team_db
    membership_driven(monkeypatch)
    app = remaining_app(factory, monkeypatch, tmp_path)
    async with client_for(app) as client:
        for (method, path, params, body), (_, path_b, params_b, body_b) in zip(run_writes("A"), run_writes("B")):
            reader = await _call(client, method, path, params, body, READER)
            assert reader.status_code == 403 and DENIED in reader.text, (path, reader.text)
            other = await _call(client, method, path_b, params_b, body_b, CONTRIBUTOR)
            assert other.status_code == 403 and DENIED in other.text, (path_b, other.text)
            own = await _call(client, method, path, params, body, CONTRIBUTOR)
            assert _authorized(own), (path, own.text)
            admin = await _call(client, method, path_b, params_b, body_b, ADMIN)
            assert _authorized(admin), (path_b, admin.text)
        for (method, path, params, body), (_, path_z, params_z, body_z) in zip(accept_writes("A"),
                                                                                 accept_writes("Z")):
            for headers in (READER, CONTRIBUTOR):
                refused = await _call(client, method, path, params, body, headers)
                assert refused.status_code == 403 and DENIED in refused.text, (path, refused.text)
            reviewer = await _call(client, method, path, params, body, REVIEWER)
            assert _authorized(reviewer), (path, reviewer.text)
            # A repository no project owns is the administrator's alone.
            assert (await _call(client, method, path_z, params_z, body_z, REVIEWER)).status_code == 403
            admin = await _call(client, method, path_z, params_z, body_z, ADMIN)
            assert _authorized(admin), (path_z, admin.text)
        # A global skill, or a learning without a repository, applies everywhere; an outcome for a task
        # that names no project's repository (or no task at all) has no repository: administrator only.
        for path, body in (("/skills", {"name": "n", "description": "d", "is_global": True,
                                        "repo_scope": "/synthetic/A"}),
                           ("/learnings", {"statement": "s"}),
                           ("/skills/1/outcomes", outcome_body(TASK_Z)),
                           ("/skills/1/outcomes", outcome_body(str(uuid.uuid4()))),
                           ("/skills/1/outcomes", outcome_body("not-a-uuid"))):
            assert (await client.post(path, json=body, headers=CONTRIBUTOR)).status_code == 403, path
            assert _authorized(await client.post(path, json=body, headers=ADMIN)), path


@pytest.mark.asyncio
async def test_team_brain_wide_routes_need_a_team_administrator(team_db, monkeypatch, tmp_path):
    factory, _, _ = team_db
    membership_driven(monkeypatch)
    app = remaining_app(factory, monkeypatch, tmp_path)
    async with client_for(app) as client:
        for method, path, body in ADMIN_ONLY:
            # Naming one's own repository does not open a Brain-wide route.
            for headers in (READER, REVIEWER):
                refused = await client.request(method, path, json=body, headers=headers,
                                               params={"repository_id": "1", "repo_path": "/synthetic/A"})
                assert refused.status_code == 403 and ADMIN_REQUIRED in refused.text, (path, refused.text)
            allowed = await client.request(method, path, json=body, headers=ADMIN)
            assert _authorized(allowed), (path, allowed.text)
        # The sign-in flow itself stays reachable to members.
        assert (await client.get("/api/web-login", headers=READER)).status_code == 200


def _memory_rows():
    """Global rows (no path, ``system_global``) plus one row per repository, spelled as clients send them."""
    return [SimpleNamespace(id=i, repo_path=path) for i, path in
            enumerate((None, "system_global", "/synthetic/A/", "/synthetic/B", "/synthetic/unattached"), 1)]


@pytest.mark.asyncio
async def test_team_memory_listings_keep_global_rows_and_readable_repositories(team_db, monkeypatch, tmp_path):
    factory, _, _ = team_db
    membership_driven(monkeypatch)
    app = remaining_app(factory, monkeypatch, tmp_path)
    monkeypatch.setattr(core_memory.DecisionStore, "list_decisions", AsyncMock(side_effect=lambda: _memory_rows()))
    monkeypatch.setattr(core_memory.RuleStore, "list_rules", AsyncMock(side_effect=lambda: _memory_rows()))
    async with client_for(app) as client:
        for path in ("/decisions", "/rules"):
            for headers in (READER, CONTRIBUTOR, REVIEWER):
                response = await client.get(path, headers=headers)
                assert response.status_code == 200, (path, response.text)
                assert [row["id"] for row in response.json()] == [1, 2, 3], (path, response.text)
            # The administrator sees every row, including repositories no project owns.
            response = await client.get(path, headers=ADMIN)
            assert [row["id"] for row in response.json()] == [1, 2, 3, 4, 5], (path, response.text)
    await revoke_everyone(factory)
    async with client_for(app) as client:
        for path in ("/decisions", "/rules"):
            assert len((await client.get(path, headers=READER)).json()) == 5, path


@pytest.mark.asyncio
async def test_team_query_scope_counts_only_the_keys_the_route_declares(team_db, monkeypatch, tmp_path):
    factory, _, _ = team_db
    membership_driven(monkeypatch)
    app = remaining_app(factory, monkeypatch, tmp_path)
    async with client_for(app) as client:
        # /harness/tasks filters by repo_path only: an id for one's own repository would otherwise
        # authorize repository A while the handler lists every repository's tasks.
        response = await client.get("/harness/tasks", params={"repository_id": "1"}, headers=READER)
        assert response.status_code == 403, response.text
        response = await client.get("/late-interaction/status",
                                    params={"repository_id": "1", "repo_path": "/synthetic/B"}, headers=READER)
        assert response.status_code == 403, response.text
        # A route that declares neither key checks its own object and still accepts either.
        response = await client.get("/insights/impact/run-a", params={"repository_id": "1"}, headers=READER)
        assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_team_telegram_bridge_answers_only_for_readable_repositories(team_db, monkeypatch, tmp_path):
    factory, _, _ = team_db
    membership_driven(monkeypatch)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_API_KEY", "fixture-legacy")
    app = remaining_app(factory, monkeypatch, tmp_path)
    async with client_for(app) as client:
        own = await client.post("/v1/assistant/reply", json={"question": "q", "project_slug": "A"},
                                headers={"Authorization": "Bearer fixture-1"})
        assert own.status_code == 200 and own.json()["answer"].endswith("fine"), own.text
        other = await client.post("/v1/assistant/reply", json={"question": "q", "project_slug": "B"},
                                  headers={"Authorization": "Bearer fixture-1"})
        assert other.status_code == 200 and DENIED in other.json()["answer"], other.text
        assert "fine" not in other.json()["answer"]
        for path in ("/v1/assistant/reply", "/v1/intake/capture"):
            body = {"question": "q", "project_slug": "A"}
            shared = await client.post(path, json=body, headers={"Authorization": "Bearer fixture-legacy"})
            assert shared.status_code == 403 and NAMED in shared.text, (path, shared.text)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_API_KEY", "")
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    async with client_for(app) as client:
        dev_open = await client.post("/v1/intake/capture", json={})
        assert dev_open.status_code == 401 and NAMED in dev_open.text


@pytest.mark.asyncio
async def test_team_state_refuses_the_shared_key_and_dev_open(team_db, monkeypatch, tmp_path):
    factory, _, _ = team_db
    membership_driven(monkeypatch)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_API_KEY", "fixture-legacy")
    app = remaining_app(factory, monkeypatch, tmp_path)
    probes = [*scoped_reads("A"), *run_writes("A"), *accept_writes("A")]
    probes += [(method, path, {}, body) for method, path, body in (*MEMBER_FILTERED, *ADMIN_ONLY)]
    async with client_for(app) as client:
        for method, path, params, body in probes:
            response = await _call(client, method, path, params, body, LEGACY)
            assert response.status_code == 403 and NAMED in response.text, (path, response.text)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_API_KEY", "")
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    async with client_for(app) as client:
        for method, path, params, body in probes[:3]:
            response = await _call(client, method, path, params, body, {})
            assert response.status_code == 401, (path, response.text)


@pytest.mark.asyncio
async def test_solo_state_is_unchanged(team_db, monkeypatch, tmp_path):
    factory, _, _ = team_db
    membership_driven(monkeypatch)
    await revoke_everyone(factory)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_API_KEY", "fixture-legacy")
    app = remaining_app(factory, monkeypatch, tmp_path)
    async with client_for(app) as client:
        for headers in (LEGACY, READER):
            for method, path, params, body in (*scoped_reads("A"), *scoped_reads("B")):
                response = await _call(client, method, path, params, body, headers)
                assert response.status_code == 200, (path, response.text)
            for method, path, params, body in (*run_writes("B"), *accept_writes("B")):
                assert _authorized(await _call(client, method, path, params, body, headers)), path
            for method, path, body in (*MEMBER_FILTERED, *ADMIN_ONLY):
                assert _authorized(await client.request(method, path, json=body, headers=headers)), path
            # Unscoped calls keep working in solo state, as before.
            assert (await client.get("/harness/tasks", headers=headers)).status_code == 200
            assert (await client.get("/jobs/job-global", headers=headers)).status_code == 200
            assert (await client.get("/insights/impact/run-b", headers=headers)).status_code == 200
            reply = await client.post("/v1/assistant/reply", json={"question": "q", "project_slug": "B"},
                                      headers={"X-API-Key": headers["X-API-Key"]})
            assert reply.json()["answer"].endswith("fine"), reply.text
    monkeypatch.setattr(settings, "PROJECT_BRAIN_API_KEY", "")
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    async with client_for(app) as client:
        assert (await client.get(f"/harness/tasks/{TASK_B}")).status_code == 200
        assert (await client.get("/decisions")).status_code == 200
        assert (await client.post("/v1/intake/capture", json={})).status_code == 200
