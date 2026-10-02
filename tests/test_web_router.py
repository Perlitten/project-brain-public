"""/api/web/* — JSON read surface for the separate web UI (apps/web).

Pure shaping/aggregation helpers are tested directly; endpoints are driven
through TestClient with every DB/graph/Redis loader patched at the router.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

with (
    patch("sqlalchemy.ext.asyncio.create_async_engine"),
    patch("redis.asyncio.from_url"),
    patch("neo4j.AsyncGraphDatabase.driver"),
):
    from apps.api.main import app

from fastapi.testclient import TestClient

import apps.api.routers.web as web
from brain.insights.drift_rules import DriftRule

client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)

REPO = SimpleNamespace(id=3, name="brain", path="/repos/brain")
CONFIG = SimpleNamespace(provider="nvidia", model="m-1", dimension=1024)


# ---- pure helpers ------------------------------------------------------


def test_module_of_uses_first_two_directories():
    assert web.module_of("brain/search/hybrid.py") == "brain/search"
    assert web.module_of("brain/search/deep/x.py") == "brain/search"
    assert web.module_of("apps\\api\\main.py") == "apps/api"
    assert web.module_of("tests/test_x.py") == "tests"
    assert web.module_of("README.md") == "(root)"


def test_pct_never_rounds_incomplete_to_100():
    assert web._pct(9999, 10000) == 99.9
    assert web._pct(10, 10) == 100.0
    assert web._pct(1, 3) == 33.3
    assert web._pct(1, 0) is None


def test_parse_context_pack_markdown_reads_budget_and_files():
    text = (
        "# Context Pack\n"
        "- **Token Budget Mode**: balanced\n"
        "## Retrieval\n"
        "### Files\n"
        "- [brain/a.py](brain/a.py) score 0.9\n"
        "- [brain/b.py](brain/b.py) score 0.8\n"
        "- [ ] checklist item\n"
        "### Symbols\n"
        "- [Foo](brain/a.py#L1)\n"
    )
    assert web.parse_context_pack_markdown(text) == {"budget": "balanced", "files": 2}
    assert web.parse_context_pack_markdown("") == {"budget": None, "files": 0}


def test_shape_context_pack_marks_stale_against_latest_index(tmp_path):
    pack_file = tmp_path / "pack.md"
    pack_file.write_text("- **Token Budget Mode**: deep\n### Files\n- [a](a)\n", encoding="utf-8")
    created = datetime(2026, 9, 1, tzinfo=timezone.utc)
    pack = SimpleNamespace(id=5, task_description="fix x", created_at=created, path=str(pack_file))
    shaped = web.shape_context_pack(pack, pack_file, created + timedelta(hours=1))
    assert shaped["stale"] is True
    assert shaped["budget"] == "deep"
    assert shaped["files"] == 1
    assert shaped["tokens"] and shaped["tokens"] > 0
    assert shaped["consumer"] is None
    assert shaped["available"] is True

    missing = web.shape_context_pack(pack, None, None)
    assert missing["available"] is False
    assert missing["stale"] is None
    assert missing["tokens"] is None


def test_aggregate_module_edges_weights_and_violations():
    rules = [
        DriftRule(
            rule_id="T-1",
            name="api must not import workers",
            description="",
            severity="high",
            source_layer_pattern="apps/api/",
            forbidden_import_pattern="brain.workers",
            remediation_guidance="",
        )
    ]
    edges = web.aggregate_module_edges(
        [
            ("apps/api/a.py", "brain/search/x.py"),
            ("apps/api/b.py", "brain/search/y.py"),
            ("apps/api/c.py", "brain/workers/q.py"),
            ("brain/search/x.py", "brain/search/y.py"),  # self-edge: skipped
        ],
        rules,
    )
    assert edges == [
        {"from": "apps/api", "to": "brain/search", "weight": 2, "violation": False},
        {"from": "apps/api", "to": "brain/workers", "weight": 1, "violation": True},
    ]


def test_aggregate_modules_classifies_chunks():
    rows = [
        ("brain/search/a.py", 1, 1024, "nvidia", "m-1"),  # current
        ("brain/search/a.py", None, None, None, None),  # missing
        ("brain/search/b.py", 2, 1024, "nvidia", "old"),  # stale model -> outdated
        ("brain/search/b.py", 3, 768, "nvidia", "m-1"),  # wrong dim -> outdated
        ("reports/out/r.md", 4, 1024, "nvidia", "m-1"),  # excluded from retrieval
    ]
    modules = {m["name"]: m for m in web.aggregate_modules(rows, CONFIG)}
    assert modules["brain/search"] == {
        "name": "brain/search", "chunks": 4, "current": 1, "outdated": 2, "missing": 1, "excluded": 0,
    }
    assert modules["reports/out"]["excluded"] == 1
    assert modules["reports/out"]["current"] == 0


def test_shape_index_run_derives_completeness_and_duration():
    started = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    run = SimpleNamespace(
        id=9, repository_id=3, commit_hash="abc123", status="COMPLETED",
        file_counts={"discovered": 110, "excluded": 10, "indexed": 40, "failed": 5},
        progress={"chunks": {"processed": 900}},
        verification={},
        started_at=started, completed_at=started + timedelta(seconds=90),
    )
    shaped = web.shape_index_run(run)
    assert shaped["status"] == "completed"
    assert shaped["files"] == 110
    assert shaped["changed"] == 40
    assert shaped["chunks"] == 900
    assert shaped["completeness"] == 0.95
    assert shaped["duration_seconds"] == 90.0
    assert shaped["trigger"] is None
    assert shaped["started_at"].startswith("2026-09-01T10:00:00")


def test_shape_job_duration_and_detail():
    done = web.shape_job({
        "id": "j1", "type": "reindex", "status": "completed",
        "created_at": "2026-09-01T10:00:00+00:00", "updated_at": "2026-09-01T10:00:30+00:00",
        "result": {"message": "ok"},
    })
    assert done["kind"] == "reindex"
    assert done["duration"] == 30.0
    assert done["detail"] == "ok"

    running = web.shape_job({
        "id": "j2", "type": "benchmark", "status": "processing",
        "created_at": "2026-09-01T10:00:00+00:00", "updated_at": "2026-09-01T10:00:30+00:00",
        "error": "x" * 400,
    })
    assert running["duration"] is None
    assert len(running["detail"]) == 300


def test_report_kind_and_shape():
    assert web.report_kind("security_audit_2026.md") == "audit"
    assert web.report_kind("impact-brain.md") == "impact"
    assert web.report_kind("diff_review.md") == "diff review"
    assert web.report_kind("weekly.md") == "report"
    shaped = web.shape_report({"name": "impact-brain.md", "created_at": "2026-09-01 10:00:00", "size": 512})
    assert shaped == {
        "id": "impact-brain.md", "title": "impact-brain", "kind": "impact",
        "created_at": "2026-09-01T10:00:00", "size_bytes": 512,
    }


# ---- endpoints ---------------------------------------------------------


def test_reports_endpoint():
    raw = [{"name": "a_audit.md", "created_at": "2026-09-01 10:00:00", "size": 10}] * 3
    with patch.object(web, "get_reports_list", return_value=raw):
        response = client.get("/api/web/reports?limit=2")
    assert response.status_code == 200
    reports = response.json()["reports"]
    assert len(reports) == 2
    assert reports[0]["kind"] == "audit"


def test_reports_endpoint_unreadable_folder_is_503():
    with patch.object(web, "get_reports_list", side_effect=OSError("denied")):
        response = client.get("/api/web/reports")
    assert response.status_code == 503


def test_health_endpoint_shape():
    services = {"postgres": {"status": "healthy"}}
    with (
        patch.object(web, "check_health", AsyncMock(return_value=dict(services))),
        patch.object(web, "check_n8n_health", AsyncMock(return_value={"status": "healthy"})),
        patch.object(web, "get_n8n_workflows", AsyncMock(return_value={
            "workflows": [{"name": "nightly"}], "source": "repo", "api_status": "disabled",
            "api_error": None, "active_workflows_count": 1,
        })),
        patch.object(web, "get_recent_brain_jobs", AsyncMock(return_value={
            "jobs": [{"id": "j", "type": "reindex", "status": "failed", "error": "boom"}], "error": None,
        })),
        patch.object(web, "get_self_diagnosis_status", AsyncMock(return_value={"status": "ok"})),
    ):
        response = client.get("/api/web/health")
    assert response.status_code == 200
    body = response.json()
    assert set(body["services"]) == {"postgres", "n8n"}
    orchestration = body["orchestration"]
    assert orchestration["workflow_source"] == "repo"
    assert orchestration["active_workflows_count"] == 1
    assert orchestration["recent_jobs"][0]["detail"] == "boom"
    assert orchestration["diagnostics"] == {"status": "ok"}


def test_module_graph_endpoint():
    with (
        patch.object(web, "_resolve_repository", AsyncMock(return_value=REPO)),
        patch.object(web, "load_file_import_edges", AsyncMock(return_value=[
            ("apps/api/a.py", "brain/search/x.py"),
        ])),
    ):
        response = client.get("/api/web/module-graph")
    assert response.status_code == 200
    body = response.json()
    assert body["repository"] == {"id": 3, "name": "brain", "path": "/repos/brain"}
    assert body["edges"] == [{"from": "apps/api", "to": "brain/search", "weight": 1, "violation": False}]
    assert body["truncated"] is False


def test_module_graph_graph_store_down_is_503():
    with (
        patch.object(web, "_resolve_repository", AsyncMock(return_value=REPO)),
        patch.object(web, "load_file_import_edges", AsyncMock(side_effect=RuntimeError("neo4j down"))),
    ):
        response = client.get("/api/web/module-graph")
    assert response.status_code == 503


def test_repo_scoped_endpoints_without_repository_return_empty():
    with patch.object(web, "_resolve_repository", AsyncMock(return_value=None)):
        assert client.get("/api/web/index-runs").json() == {"repository": None, "runs": []}
        assert client.get("/api/web/modules").json() == {"repository": None, "modules": []}
        assert client.get("/api/web/module-graph").json() == {
            "repository": None, "edges": [], "truncated": False,
        }


def test_web_endpoints_require_api_key_when_configured():
    with patch("apps.api.auth.settings") as mock_settings:
        mock_settings.PROJECT_BRAIN_API_KEY = "test-secret-key"
        response = client.get("/api/web/reports")
    assert response.status_code == 401


# ---- retired dashboard / service root ------------------------------------


def test_root_advertises_web_ui():
    with patch("apps.api.main.settings.BRAIN_WEB_URL", None):
        assert client.get("/").json() == {"service": "project-brain", "ui": None}
    with patch("apps.api.main.settings.BRAIN_WEB_URL", " https://ui.example.com "):
        assert client.get("/").json()["ui"] == "https://ui.example.com"


def test_dashboard_is_404_without_web_url():
    with patch("apps.api.main.settings.BRAIN_WEB_URL", None):
        response = client.get("/dashboard/indexing")
    assert response.status_code == 404
    assert "BRAIN_WEB_URL" in response.json()["detail"]


def test_dashboard_redirects_to_web_url():
    with patch("apps.api.main.settings.BRAIN_WEB_URL", "https://ui.example.com"):
        root = client.get("/dashboard")
        nested = client.get("/dashboard/indexing")
    assert root.status_code == 301
    assert root.headers["location"].startswith("https://ui.example.com")
    assert nested.status_code == 301
    assert nested.headers["location"].startswith("https://ui.example.com")
