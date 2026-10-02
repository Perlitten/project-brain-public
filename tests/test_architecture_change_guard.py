"""Comprehensive Test Suite for Architectural Change Guard (Phase 12)."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from apps.api.auth import require_api_key
from apps.api.main import app
from brain.insights.drift_analyzer import DriftFinding
from brain.insights.drift_cli import generate_sarif_report
from brain.insights.drift_enforcement import (
    EXIT_POLICY_VIOLATION,
    EXIT_SUCCESS,
    ArchitectureChangeGuardEngine,
)
from brain.insights.drift_policy import ArchitecturePolicy
from brain.insights.drift_waivers import DriftWaiverManager
from brain.insights.git_diff import GitDiffEngine
from tests.fixtures.git_drift_fixtures import (
    setup_clean_git_fixture,
    setup_critical_violation_git_fixture,
)


def test_git_diff_engine_revision_validation(tmp_path):
    repo_dir = tmp_path / "git_repo"
    base_sha, cand_sha = setup_clean_git_fixture(repo_dir)

    diff_engine = GitDiffEngine(repo_dir)
    assert diff_engine.resolve_revision("HEAD") == cand_sha
    assert diff_engine.resolve_revision(base_sha) == base_sha

    with pytest.raises(ValueError, match="Invalid Git revision syntax"):
        diff_engine.resolve_revision("HEAD; rm -rf /")


def test_git_diff_engine_changeset_computation(tmp_path):
    repo_dir = tmp_path / "git_repo"
    base_sha, cand_sha = setup_clean_git_fixture(repo_dir)

    diff_engine = GitDiffEngine(repo_dir)
    changes = diff_engine.get_changeset(base_sha, cand_sha)
    assert len(changes) == 1
    assert changes[0].new_path == "another_clean.py"
    assert changes[0].change_type == "added"


def test_architecture_policy_validation(tmp_path):
    policy_file = tmp_path / "architecture-policy.yaml"
    policy_file.write_text(
        "version: 1\n"
        "enforcement:\n"
        "  mode: fail\n"
        "  fail_on:\n"
        "    states: [new]\n"
        "    severities: [critical]\n"
        "  max_new_warnings: 2\n",
        encoding="utf-8",
    )

    policy = ArchitecturePolicy.load_from_yaml(policy_file)
    assert policy.enforcement.mode == "fail"
    assert policy.enforcement.max_new_warnings == 2
    assert "new" in policy.enforcement.fail_on_states

    # Test invalid policy
    invalid_policy_file = tmp_path / "invalid.yaml"
    invalid_policy_file.write_text("version: 999\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Unsupported architecture policy version"):
        ArchitecturePolicy.load_from_yaml(invalid_policy_file)


def test_drift_waiver_matching_and_expiration(tmp_path):
    waiver_file = tmp_path / "drift-waivers.yaml"
    waiver_file.write_text(
        "version: 1\n"
        "waivers:\n"
        "  - id: WAIVER-001\n"
        "    rule_id: DRIFT-001\n"
        "    fingerprint: fp123\n"
        "    owner: platform\n"
        "    reason: Migration bridge\n"
        "    created_at: '2026-01-01'\n"
        "    expires_at: '2099-01-01'\n"
        "  - id: WAIVER-EXPIRED\n"
        "    rule_id: DRIFT-002\n"
        "    owner: platform\n"
        "    reason: Old exception\n"
        "    created_at: '2020-01-01'\n"
        "    expires_at: '2020-01-02'\n",
        encoding="utf-8",
    )

    mgr = DriftWaiverManager(waiver_file)
    waivers = mgr.load_waivers()
    assert len(waivers) == 2

    active_finding = DriftFinding(
        fingerprint="fp123",
        rule_id="DRIFT-001",
        rule_name="Rule",
        file_path="file.py",
        line_number=10,
        containing_symbol="global",
        imported_module="mod",
        severity="warning",
        description="desc",
        remediation_guidance="remediation",
    )
    status, applied = mgr.evaluate_finding_waiver(active_finding, waivers)
    assert status == "waived"
    assert applied.id == "WAIVER-001"

    expired_finding = DriftFinding(
        fingerprint="fp456",
        rule_id="DRIFT-002",
        rule_name="Rule2",
        file_path="file2.py",
        line_number=5,
        containing_symbol="global",
        imported_module="mod2",
        severity="warning",
        description="desc",
        remediation_guidance="remediation",
    )
    status2, applied2 = mgr.evaluate_finding_waiver(expired_finding, waivers)
    assert status2 == "waiver_expired"


def test_change_guard_engine_clean_repo(tmp_path):
    repo_dir = tmp_path / "git_repo"
    base_sha, cand_sha = setup_clean_git_fixture(repo_dir)

    engine = ArchitectureChangeGuardEngine(repo_dir)
    res = engine.evaluate_change_guard(base_sha, cand_sha)
    assert res.decision == "pass"
    assert res.exit_code == EXIT_SUCCESS


def test_change_guard_engine_critical_violation(tmp_path):
    repo_dir = tmp_path / "git_repo"
    base_sha, cand_sha = setup_critical_violation_git_fixture(repo_dir)

    engine = ArchitectureChangeGuardEngine(repo_dir)
    res = engine.evaluate_change_guard(base_sha, cand_sha)
    assert res.decision == "fail"
    assert res.exit_code == EXIT_POLICY_VIOLATION
    assert len(res.policy_reasons) > 0


def test_sarif_report_generation(tmp_path):
    repo_dir = tmp_path / "git_repo"
    base_sha, cand_sha = setup_critical_violation_git_fixture(repo_dir)

    engine = ArchitectureChangeGuardEngine(repo_dir)
    res = engine.evaluate_change_guard(base_sha, cand_sha)

    sarif = generate_sarif_report(res)
    assert sarif["version"] == "2.1.0"
    assert len(sarif["runs"]) == 1
    assert sarif["runs"][0]["tool"]["driver"]["name"] == "Project Brain Architecture Change Guard"


def test_api_check_policy_waivers_endpoints(tmp_path):
    repo_dir = tmp_path / "git_repo"
    base_sha, cand_sha = setup_clean_git_fixture(repo_dir)

    app.dependency_overrides[require_api_key] = lambda: None
    client = TestClient(app)

    try:
        with patch("apps.api.routers.drift.settings.TARGET_REPO_PATH", str(repo_dir)):
            # /check endpoint
            resp_check = client.post(f"/insights/architectural-drift/check?repo_path={repo_dir}&base_rev={base_sha}&candidate_rev={cand_sha}")
            assert resp_check.status_code == 200
            assert resp_check.json()["decision"] == "pass"

            # /policy endpoint
            resp_pol = client.get(f"/insights/architectural-drift/policy?repo_path={repo_dir}")
            assert resp_pol.status_code == 200
            assert resp_pol.json()["status"] == "success"

            # /waivers endpoint
            resp_w = client.get(f"/insights/architectural-drift/waivers?repo_path={repo_dir}")
            assert resp_w.status_code == 200
            assert resp_w.json()["status"] == "success"
    finally:
        app.dependency_overrides.clear()
