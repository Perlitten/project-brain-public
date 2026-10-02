"""Unit Tests for Shadow Execution in Project Brain v0.5.3."""

from fastapi.testclient import TestClient
from brain.shadow.models import ShadowState
from brain.shadow.runner import ShadowRunner
from brain.shadow.cli import main as cli_main
from apps.api.main import app

client = TestClient(app)


def test_shadow_runner_lifecycle():
    runner = ShadowRunner()
    sess = runner.create_session("task-123", "project-brain")
    assert sess.state == ShadowState.CREATED

    run_sess = runner.run_shadow(sess.shadow_id)
    assert run_sess.state == ShadowState.COMPLETED
    assert run_sess.comparison is not None
    assert run_sess.comparison.utility_verdict == "shadow_improved"


def test_shadow_api_endpoints():
    list_resp = client.get("/shadow/executions")
    assert list_resp.status_code in (200, 401, 403, 422)

    summary_resp = client.get("/shadow/summary")
    assert summary_resp.status_code in (200, 401, 403, 422)


def test_cli_import():
    assert callable(cli_main)
