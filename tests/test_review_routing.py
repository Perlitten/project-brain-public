"""Unit & integration test suite for Reviewer Routing, Review Package, and Local Queue (Workstream D)."""

from unittest.mock import patch

from fastapi.testclient import TestClient

from apps.api.auth import require_api_key
from apps.api.main import app
from brain.insights.review_package import ReviewPackageWriter
from brain.insights.review_queue import ReviewQueueManager


def test_review_package_writer(tmp_path):
    pkg_dir = tmp_path / "review_package"
    manifest = ReviewPackageWriter.create_package(
        output_dir=pkg_dir,
        repository="test-repo",
        base_rev="base123",
        cand_rev="cand456",
        result_dict={"decision": "pass"},
        risk_dict={"numeric_score": 10.0},
        comment_md="## Review Pass",
        sarif_dict={"version": "2.1.0"},
        annotations_list=[],
    )

    assert manifest.repository == "test-repo"
    assert manifest.base_revision == "base123"
    assert manifest.candidate_revision == "cand456"
    assert "result.json" in manifest.checksums
    assert (pkg_dir / "manifest.json").exists()


def test_review_queue_manager_lifecycle_and_supersession(tmp_path):
    queue_file = tmp_path / "review_queue.jsonl"
    mgr = ReviewQueueManager(queue_file)

    # 1. Enqueue item
    item1 = mgr.enqueue_review(
        repository="repo-a",
        base_rev="rev1",
        cand_rev="rev2",
        decision="fail",
        risk_category="high",
        risk_score=65.0,
        owners=["@owner-1"],
        package_path=str(tmp_path / "pkg1"),
        run_fingerprint="fp-100",
    )
    assert item1.state == "pending"
    assert len(mgr.list_reviews()) == 1

    # 2. Acknowledge
    ack_item = mgr.update_state(item1.review_id, "acknowledged")
    assert ack_item.state == "acknowledged"

    # 3. Re-evaluate same run_fingerprint -> should supersede previous acknowledged review
    item2 = mgr.enqueue_review(
        repository="repo-a",
        base_rev="rev1",
        cand_rev="rev2",
        decision="pass",
        risk_category="low",
        risk_score=10.0,
        owners=["@owner-1"],
        package_path=str(tmp_path / "pkg2"),
        run_fingerprint="fp-100",
    )
    assert item2.state == "pending"

    # Check updated list
    all_reviews = mgr.list_reviews()
    assert len(all_reviews) == 2
    old_rev = mgr.get_review(item1.review_id)
    assert old_rev.state == "superseded"


def test_api_review_queue_endpoints(tmp_path):
    queue_file = tmp_path / ".brain" / "review_queue.jsonl"
    mgr = ReviewQueueManager(queue_file)
    item = mgr.enqueue_review(
        repository="api-repo",
        base_rev="b1",
        cand_rev="c1",
        decision="pass",
        risk_category="low",
        risk_score=0.0,
        owners=["@lead"],
        package_path="/pkg",
        run_fingerprint="fp-200",
    )

    app.dependency_overrides[require_api_key] = lambda: None
    client = TestClient(app)

    try:
        with patch("apps.api.routers.drift.settings.TARGET_REPO_PATH", str(tmp_path)):
            # GET /reviews
            r1 = client.get(f"/insights/architectural-drift/reviews?repo_path={tmp_path}")
            assert r1.status_code == 200
            assert r1.json()["total_reviews"] == 1

            # GET /reviews/{id}
            r2 = client.get(f"/insights/architectural-drift/reviews/{item.review_id}?repo_path={tmp_path}")
            assert r2.status_code == 200
            assert r2.json()["review"]["state"] == "pending"

            # POST /reviews/{id}/approve
            r3 = client.post(f"/insights/architectural-drift/reviews/{item.review_id}/approve?repo_path={tmp_path}")
            assert r3.status_code == 200
            assert r3.json()["review"]["state"] == "approved"
    finally:
        app.dependency_overrides.clear()
