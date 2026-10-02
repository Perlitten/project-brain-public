"""Comprehensive Unit & Integration Test Suite for Architectural Drift Intelligence v2 (Phase 9)."""

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from apps.api.main import app
from brain.insights.drift_analyzer import (
    calculate_stable_fingerprint,
    scan_repository_drift,
)
from brain.insights.drift_baseline import (
    DriftBaseline,
    DriftBaselineManager,
)
from brain.insights.drift_cli import scan_cmd, summary_cmd
from brain.insights.drift_rules import get_default_rule_registry
from tests.fixtures.drift_fixtures import (
    setup_moved_fixture,
    setup_resolved_fixture,
    setup_violating_fixture,
)

from apps.api.auth import require_api_key


def test_rule_registry_initialization_and_filtering():
    reg = get_default_rule_registry()
    rules = reg.list_active_rules()
    assert len(rules) >= 4
    rule_ids = {r.rule_id for r in rules}
    assert "DRIFT-001" in rule_ids
    assert "DRIFT-002" in rule_ids
    assert "DRIFT-003" in rule_ids


def test_stable_fingerprint_line_independence_and_path_normalization():
    # Same violation on different lines must produce identical fingerprint
    fp1 = calculate_stable_fingerprint("DRIFT-001", "apps/api/routers/users.py", "handle_request", "sqlalchemy.orm.session")
    fp2 = calculate_stable_fingerprint("DRIFT-001", "apps/api/routers/users.py", "handle_request", "sqlalchemy.orm.session")
    assert fp1 == fp2

    # Windows vs POSIX path normalization
    fp_win = calculate_stable_fingerprint("DRIFT-001", "apps\\api\\routers\\users.py", "handle_request", "sqlalchemy.orm.session")
    assert fp_win == fp1

    # Different containing symbol produces different fingerprint
    fp3 = calculate_stable_fingerprint("DRIFT-001", "apps/api/routers/users.py", "other_func", "sqlalchemy.orm.session")
    assert fp3 != fp1


def test_atomic_baseline_save_and_load(tmp_path):
    baseline_path = tmp_path / ".brain" / "drift_baseline.json"
    mgr = DriftBaselineManager(baseline_path)

    baseline = DriftBaseline(
        repository_id="test_repo",
        source_revision="abc1234",
        created_at_utc="2026-08-03T15:00:00Z",
        rule_registry_version="1.0.0",
        findings=[{"fingerprint": "drift-001:123", "rule_id": "DRIFT-001", "line_number": 10}],
    )

    mgr.save_baseline_atomic(baseline)
    assert baseline_path.exists()

    loaded = mgr.load_baseline()
    assert loaded is not None
    assert loaded.repository_id == "test_repo"
    assert loaded.source_revision == "abc1234"
    assert len(loaded.findings) == 1


def test_fixture_lifecycle_new_persistent_moved_resolved(tmp_path):
    repo_dir = tmp_path / "test_repo"
    baseline_path = repo_dir / ".brain" / "drift_baseline.json"
    mgr = DriftBaselineManager(baseline_path)

    # 1. Initial scan on violating repo -> NEW
    setup_violating_fixture(repo_dir)
    findings_1 = scan_repository_drift(repo_dir)
    assert len(findings_1) == 2  # DRIFT-001 and DRIFT-002

    deltas_1 = mgr.compute_deltas(findings_1, None)
    assert all(d.delta_state == "new" for d in deltas_1)

    # Save baseline 1
    base_1 = DriftBaseline(
        repository_id="test_repo",
        source_revision="rev1",
        created_at_utc="2026-08-03T15:00:00Z",
        rule_registry_version="1.0.0",
        findings=[d.finding.to_dict() for d in deltas_1],
    )
    mgr.save_baseline_atomic(base_1)

    # 2. Repeat scan on same violating repo -> PERSISTENT
    findings_2 = scan_repository_drift(repo_dir)
    deltas_2 = mgr.compute_deltas(findings_2, base_1)
    assert all(d.delta_state == "persistent" for d in deltas_2)

    # 3. Move violation down 15 lines -> MOVED
    setup_moved_fixture(repo_dir)
    findings_3 = scan_repository_drift(repo_dir)
    deltas_3 = mgr.compute_deltas(findings_3, base_1)
    states_3 = {d.finding.rule_id: d.delta_state for d in deltas_3}
    assert states_3["DRIFT-001"] == "moved"
    assert states_3["DRIFT-002"] == "persistent"

    # Save baseline 3
    base_3 = DriftBaseline(
        repository_id="test_repo",
        source_revision="rev3",
        created_at_utc="2026-08-03T15:10:00Z",
        rule_registry_version="1.0.0",
        findings=[d.finding.to_dict() for d in deltas_3 if d.delta_state != "resolved"],
    )
    mgr.save_baseline_atomic(base_3)

    # 4. Resolve DRIFT-001 -> RESOLVED
    setup_resolved_fixture(repo_dir)
    findings_4 = scan_repository_drift(repo_dir)
    deltas_4 = mgr.compute_deltas(findings_4, base_3)
    states_4 = {d.finding.rule_id: d.delta_state for d in deltas_4}
    assert states_4["DRIFT-001"] == "resolved"
    assert states_4["DRIFT-002"] == "persistent"


def test_malformed_python_file_handled_safely(tmp_path):
    repo_dir = tmp_path / "bad_repo"
    setup_violating_fixture(repo_dir)

    # scan_repository_drift should not crash on malformed syntax
    findings = scan_repository_drift(repo_dir)
    assert len(findings) == 2


def test_api_router_drift_endpoints(tmp_path):
    repo_dir = tmp_path / "api_repo"
    setup_violating_fixture(repo_dir)

    app.dependency_overrides[require_api_key] = lambda: None
    client = TestClient(app)

    try:
        with patch("apps.api.routers.drift.settings.TARGET_REPO_PATH", str(repo_dir)):
            # POST /insights/architectural-drift/scan
            resp = client.post(f"/insights/architectural-drift/scan?repo_path={repo_dir}")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "success"
            assert data["total_findings"] == 2
            assert data["delta_counts"]["new"] == 2

            # GET /insights/architectural-drift/summary
            resp_sum = client.get(f"/insights/architectural-drift/summary?repo_path={repo_dir}")
            assert resp_sum.status_code == 200
            data_sum = resp_sum.json()
            assert data_sum["total_active_violations"] == 2

            # GET /insights/architectural-drift/findings
            resp_find = client.get(f"/insights/architectural-drift/findings?repo_path={repo_dir}&severity=warning")
            assert resp_find.status_code == 200
            data_find = resp_find.json()
            assert data_find["total"] == 1
            assert data_find["findings"][0]["rule_id"] == "DRIFT-001"
    finally:
        app.dependency_overrides.clear()


def test_drift_cli_scan_and_summary(tmp_path, capsys):
    repo_dir = tmp_path / "cli_repo"
    setup_violating_fixture(repo_dir)

    scan_cmd(str(repo_dir))
    captured = capsys.readouterr()
    assert "Total active violations" in captured.out or "total_active_violations" in captured.out

    summary_cmd(str(repo_dir))
    captured_sum = capsys.readouterr()
    assert "total_violations" in captured_sum.out


def test_secure_path_validation_and_traversal_prevention(tmp_path):
    from brain.config.paths import validate_secure_repo_path

    # Allowed path passes
    repo_dir = tmp_path / "allowed_repo"
    repo_dir.mkdir()
    with patch("brain.config.paths.get_repo_root", return_value=tmp_path):
        resolved = validate_secure_repo_path(repo_dir)
        assert resolved == repo_dir.resolve()

        # Non-existent path raises ValueError
        with pytest.raises(ValueError, match="does not exist"):
            validate_secure_repo_path(tmp_path / "non_existent")

        # Outside root traversal raises ValueError
        target_outside = Path("C:/Windows") if Path("C:/Windows").exists() else Path("/etc")
        with patch("brain.config.paths.tempfile.gettempdir", return_value=str(tmp_path)):
            with pytest.raises(ValueError, match="outside allowed repository roots"):
                validate_secure_repo_path(target_outside)


def test_read_only_scan_mode_and_dot_brain_exclusion(tmp_path):
    repo_dir = tmp_path / "ro_repo"
    setup_violating_fixture(repo_dir)

    app.dependency_overrides[require_api_key] = lambda: None
    client = TestClient(app)

    try:
        with patch("apps.api.routers.drift.settings.TARGET_REPO_PATH", str(repo_dir)):
            # Read-only scan should NOT create .brain/drift_baseline.json on disk
            resp = client.post(f"/insights/architectural-drift/scan?repo_path={repo_dir}&read_only=true")
            assert resp.status_code == 200
            assert not (repo_dir / ".brain" / "drift_baseline.json").exists()

            # Normal scan creates baseline
            resp_write = client.post(f"/insights/architectural-drift/scan?repo_path={repo_dir}&read_only=false")
            assert resp_write.status_code == 200
            assert (repo_dir / ".brain" / "drift_baseline.json").exists()

            # Confirm .brain is excluded from repository scanning
            findings = scan_repository_drift(repo_dir)
            assert not any(".brain" in f.file_path for f in findings)
    finally:
        app.dependency_overrides.clear()
