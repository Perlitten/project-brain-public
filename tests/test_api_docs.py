"""Check documentation exposure on independently imported application instances."""

import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("environment", "override", "expected"),
    [
        ("local", None, 200),
        ("production", None, 404),
        ("local", False, 404),
        ("production", True, 200),
    ],
)
def test_api_documentation_routes(environment, override, expected):
    # Separate processes keep main's import-time routing and global settings
    # changes out of other tests. No lifespan or external services are started.
    code = f"""
from brain.config.settings import settings
settings.ENVIRONMENT = {environment!r}
settings.API_DOCS_ENABLED = {override!r}
from apps.api.main import app
from fastapi.testclient import TestClient
client = TestClient(app)
for path in ('/docs', '/redoc', '/openapi.json'):
    response = client.get(path)
    assert response.status_code == {expected}, (path, response.status_code)
assert client.get('/').status_code == 200
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
