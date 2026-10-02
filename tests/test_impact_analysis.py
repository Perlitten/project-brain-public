"""Unit & integration test suite for Deep Change-Impact Analysis (Workstream C)."""

from unittest.mock import patch

from fastapi.testclient import TestClient

from apps.api.auth import require_api_key
from apps.api.main import app
from brain.insights.impact_engine import ImpactAnalysisEngine
from brain.insights.impact_models import ImpactedEntity
from brain.insights.test_impact import TestImpactSelector


def test_test_impact_selector(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_api.py").write_text("def test_api(): pass\n", encoding="utf-8")

    entities = [
        ImpactedEntity(
            entity_id="file:apps/api/main.py",
            normalized_path="apps/api/main.py",
            entity_type="File",
            subsystem="api",
            evidence_category="direct",
            confidence="confirmed",
        )
    ]

    suggested = TestImpactSelector.select_tests(tmp_path, entities)
    assert "tests/test_api.py" in suggested


def test_impact_analysis_engine(tmp_path):
    engine = ImpactAnalysisEngine(tmp_path)

    with patch("brain.insights.git_diff.GitDiffEngine.get_changed_files", return_value=["apps/api/main.py"]):
        res = engine.analyze_impact(base_rev="HEAD~1", cand_rev="HEAD")

        assert res.directly_changed_files == ["apps/api/main.py"]
        assert len(res.impacted_entities) >= 1
        assert "api" in res.impacted_subsystems


def test_impact_api_endpoints(tmp_path):
    app.dependency_overrides[require_api_key] = lambda: None
    client = TestClient(app)

    try:
        with patch("apps.api.routers.impact.resolve_repo_path", return_value=tmp_path):
            with patch("brain.insights.git_diff.GitDiffEngine.get_changed_files", return_value=["brain/core/context_pack.py"]):
                r1 = client.post(f"/insights/impact/analyze?repo_path={tmp_path}")
                assert r1.status_code == 200
                data = r1.json()
                assert data["status"] == "success"
                run_id = data["result"]["run_id"]

                # GET /{run_id}
                r2 = client.get(f"/insights/impact/{run_id}")
                assert r2.status_code == 200

                # GET /{run_id}/tests
                r3 = client.get(f"/insights/impact/{run_id}/tests")
                assert r3.status_code == 200
                assert "suggested_tests" in r3.json()
    finally:
        app.dependency_overrides.clear()
