"""Unit & integration test suite for PR Comment Summary and CODEOWNERS Resolution (Workstream 1)."""

from unittest.mock import patch

from fastapi.testclient import TestClient

from apps.api.auth import require_api_key
from apps.api.main import app
from brain.insights.codeowners import CodeownersParser, FindingOwnershipResolver
from brain.insights.pr_risk import ArchitectureRiskCalculator
from brain.insights.pr_summary import PRMarkdownRenderer, PRSummaryModel
from tests.fixtures.git_drift_fixtures import setup_clean_git_fixture


def test_codeowners_parser_and_resolver(tmp_path):
    github_dir = tmp_path / ".github"
    github_dir.mkdir()
    codeowners_file = github_dir / "CODEOWNERS"
    codeowners_file.write_text(
        "# Global owners\n"
        "* @global-lead\n\n"
        "# API owners\n"
        "apps/api/ @api-team\n"
        "apps/api/routers/ @router-lead @api-team\n",
        encoding="utf-8",
    )

    parsed = CodeownersParser.discover_and_parse(tmp_path)
    assert parsed.file_path == ".github/CODEOWNERS"
    assert len(parsed.rules) == 3

    resolver = FindingOwnershipResolver(parsed)

    # Global match
    r1 = resolver.resolve_path_ownership("brain/core/context.py")
    assert r1.owners == ["@global-lead"]

    # Specific directory override (last matching wins)
    r2 = resolver.resolve_path_ownership("apps/api/routers/drift.py")
    assert r2.owners == ["@router-lead", "@api-team"]


def test_architecture_risk_calculator():
    clean_risk = ArchitectureRiskCalculator.calculate_risk(
        new_criticals_count=0,
        new_warnings_count=0,
        expired_waivers_count=0,
        unowned_findings_count=0,
        files_changed_count=3,
        policy_failed=False,
    )
    assert clean_risk.numeric_score == 0.0
    assert clean_risk.risk_category == "low"

    high_risk = ArchitectureRiskCalculator.calculate_risk(
        new_criticals_count=1,
        new_warnings_count=2,
        expired_waivers_count=0,
        unowned_findings_count=1,
        files_changed_count=5,
        policy_failed=True,
    )
    assert high_risk.numeric_score >= 75.0
    assert high_risk.risk_category == "critical"


def test_pr_markdown_renderer_and_comment_replacement():
    summary = PRSummaryModel(
        repository="test-repo",
        base_revision="abcdef12345",
        candidate_revision="67890abcdef",
        decision="fail",
        exit_code=2,
        policy_version="1.0.0",
        files_changed=5,
        files_scanned=3,
        new_findings_count=1,
        persistent_findings_count=0,
        moved_findings_count=0,
        resolved_findings_count=0,
        waived_count=0,
        expired_waivers_count=0,
        risk_score=75.0,
        risk_category="critical",
        risk_reasons=["1 new critical architectural violation"],
        responsible_owners=["@platform-team"],
        unowned_findings_count=0,
    )

    rendered = PRMarkdownRenderer.render_comment(summary)
    assert "<!-- project-brain:architecture-review:v1 -->" in rendered
    assert "Architecture Review: **FAIL**" in rendered
    assert "@platform-team" in rendered

    # Test comment replacement
    existing_comment = "Some human developer note.\n\n" + rendered
    summary.decision = "pass"
    summary.exit_code = 0
    updated_rendered = PRMarkdownRenderer.render_comment(summary)

    replaced = PRMarkdownRenderer.replace_or_append_comment(existing_comment, updated_rendered)
    assert "Architecture Review: **PASS**" in replaced
    assert replaced.count("<!-- project-brain:architecture-review:v1 -->") == 1


def test_api_pr_review_endpoint(tmp_path):
    repo_dir = tmp_path / "git_repo"
    base_sha, cand_sha = setup_clean_git_fixture(repo_dir)

    app.dependency_overrides[require_api_key] = lambda: None
    client = TestClient(app)

    try:
        with patch("apps.api.routers.drift.settings.TARGET_REPO_PATH", str(repo_dir)):
            resp = client.post(
                f"/insights/architectural-drift/pr-review?repo_path={repo_dir}&base_rev={base_sha}&candidate_rev={cand_sha}"
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "success"
            assert data["evaluation"]["decision"] == "pass"
            assert "markdown_comment" in data
    finally:
        app.dependency_overrides.clear()
