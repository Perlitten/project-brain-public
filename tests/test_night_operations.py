"""Unit Tests for Night Operations in Project Brain v0.5.3."""

from fastapi.testclient import TestClient
from brain.operations.nightly import NightlyOperationsManager
from brain.operations.cli import main as cli_main
from apps.api.main import app

client = TestClient(app)


def test_nightly_operations_manager():
    mgr = NightlyOperationsManager()
    inc = mgr.correlate_incident("run-1", "job-1", ["repo-a"], "Timeout")
    assert inc.affected_repository_ids == ["repo-a"]

    # High recall 1.0 is not a warning
    assert mgr.evaluate_retrieval_recall_warning(1.0, False) is None
    assert mgr.evaluate_retrieval_recall_warning(0.3, False) == "Warning: Low retrieval recall"

    # Empty unhealthy fix
    san = mgr.sanitize_unhealthy_probe_details({})
    assert san["error"] == "diagnostic_internal_error"

    # Vector repair
    job = mgr.repair_vectors("repo-a")
    assert job.post_repair_verified is True

    # Replay
    replay = mgr.replay_historical_incidents()
    assert replay["replay_status"] == "success"


def test_operations_api_endpoints():
    latest = client.get("/operations/nightly/latest")
    assert latest.status_code in (200, 401, 403, 422)


def test_cli_import():
    assert callable(cli_main)
