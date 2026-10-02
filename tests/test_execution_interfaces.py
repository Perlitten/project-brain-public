"""Tests for API and CLI interfaces of v0.5.2 Phased Execution Loop."""

from fastapi.testclient import TestClient
from apps.api.main import app
from brain.execution.cli import main as cli_main

client = TestClient(app)


def test_execution_api_router():
    # Test unauthenticated access returns 200 (if auth disabled), 401/403 or 422
    resp = client.post("/execution/sessions?repo_id=project-brain&task_category=bug_fix")
    assert resp.status_code in (200, 401, 403, 422)


def test_cli_import():
    assert callable(cli_main)
