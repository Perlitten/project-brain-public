"""Unit & integration test suite for Architectural Drift Trend Analytics."""

from unittest.mock import patch

from fastapi.testclient import TestClient

from apps.api.auth import require_api_key
from apps.api.main import app
from brain.insights.drift_analyzer import scan_repository_drift
from brain.insights.drift_baseline import DriftBaselineManager
from brain.insights.drift_trends import DriftTrendTracker
from tests.fixtures.drift_fixtures import setup_violating_fixture


def test_drift_trend_tracker_records_and_retrieves_history(tmp_path):
    trends_file = tmp_path / "drift_trends.jsonl"
    tracker = DriftTrendTracker(trends_file)

    repo_dir = tmp_path / "test_repo"
    setup_violating_fixture(repo_dir)

    findings = scan_repository_drift(repo_dir)
    baseline_mgr = DriftBaselineManager(tmp_path / "baseline.json")
    deltas = baseline_mgr.compute_deltas(findings, None)

    point1 = tracker.record_scan_event("test_repo", deltas)
    assert point1.total_violations == 2
    assert point1.new_count == 2
    assert point1.resolved_count == 0

    history = tracker.get_history()
    assert len(history) == 1
    assert history[0]["repository_id"] == "test_repo"
    assert history[0]["total_violations"] == 2


def test_api_drift_trends_endpoint(tmp_path):
    repo_dir = tmp_path / "api_trend_repo"
    setup_violating_fixture(repo_dir)

    app.dependency_overrides[require_api_key] = lambda: None
    client = TestClient(app)

    try:
        with patch("apps.api.routers.drift.settings.TARGET_REPO_PATH", str(repo_dir)):
            # Perform scan to create initial trend point
            scan_resp = client.post(f"/insights/architectural-drift/scan?repo_path={repo_dir}")
            assert scan_resp.status_code == 200

            # Query trends endpoint
            trends_resp = client.get(f"/insights/architectural-drift/trends?repo_path={repo_dir}")
            assert trends_resp.status_code == 200
            data = trends_resp.json()
            assert data["status"] == "success"
            assert data["total_points"] == 1
            assert data["history"][0]["total_violations"] == 2
    finally:
        app.dependency_overrides.clear()
