"""Unit & integration test suite for Assisted Remediation Planner (Workstream D)."""

from unittest.mock import patch

from fastapi.testclient import TestClient

from apps.api.auth import require_api_key
from apps.api.main import app
from brain.insights.remediation_manager import RemediationPlanManager
from brain.insights.remediation_package import RemediationPackageWriter
from brain.insights.remediation_strategies import RemediationStrategyRegistry


def test_remediation_plan_generation_and_freshness(tmp_path):
    target_file = tmp_path / "sample.py"
    target_file.write_text("import db\n", encoding="utf-8")

    finding = {"rule_id": "DRIFT-001", "file_path": "sample.py", "description": "Direct DB access"}
    plan = RemediationStrategyRegistry.create_plan_for_finding("test-repo", "HEAD", finding, tmp_path)

    assert plan.plan_id.startswith("plan-drift-001")
    assert len(plan.remediation_options) >= 2
    assert plan.patch_diff is not None

    mgr = RemediationPlanManager(tmp_path / ".brain")
    mgr.save_plan(plan)

    fresh, reason = mgr.validate_plan_freshness(plan.plan_id, tmp_path)
    assert fresh is True

    # Mutate file -> should invalidate freshness
    target_file.write_text("import db # changed\n", encoding="utf-8")
    fresh_after, _ = mgr.validate_plan_freshness(plan.plan_id, tmp_path)
    assert fresh_after is False
    assert mgr.get_plan(plan.plan_id).state == "invalidated"


def test_remediation_package_writer(tmp_path):
    target_file = tmp_path / "sample.py"
    target_file.write_text("x = 1\n", encoding="utf-8")
    finding = {"rule_id": "COUPLING-001", "file_path": "sample.py", "description": "Forbidden dep"}
    plan = RemediationStrategyRegistry.create_plan_for_finding("test-repo", "HEAD", finding, tmp_path)

    pkg_dir = tmp_path / "package"
    manifest = RemediationPackageWriter.create_package(pkg_dir, plan)

    assert manifest.plan_id == plan.plan_id
    assert "plan.json" in manifest.checksums
    assert (pkg_dir / "manifest.json").exists()


def test_remediation_api_endpoints(tmp_path):
    app.dependency_overrides[require_api_key] = lambda: None
    client = TestClient(app)

    try:
        with patch("apps.api.routers.remediation.resolve_repo_path", return_value=tmp_path):
            # 1. Create
            r1 = client.post(f"/insights/remediation/plans?rule_id=DRIFT-001&file_path=apps/api/main.py&repo_path={tmp_path}")
            assert r1.status_code == 200
            plan_id = r1.json()["plan"]["plan_id"]

            # 2. List
            r2 = client.get(f"/insights/remediation/plans?repo_path={tmp_path}")
            assert r2.status_code == 200
            assert r2.json()["total_plans"] == 1

            # 3. Approve
            r3 = client.post(f"/insights/remediation/plans/{plan_id}/approve?repo_path={tmp_path}")
            assert r3.status_code == 200
            assert r3.json()["plan"]["state"] == "approved"
    finally:
        app.dependency_overrides.clear()
