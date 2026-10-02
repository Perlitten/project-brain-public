"""Release identity consistency tests for Project Brain v0.9.0."""

from brain._version import RELEASE_CODENAME, __version__


def test_release_identity_consistency():
    """Verify package version, brain._version, and pyproject.toml match."""
    assert __version__ == "0.9.0"
    assert "v0.9.0" in RELEASE_CODENAME or "0.9.0" in RELEASE_CODENAME


def test_api_version_endpoint():
    """Verify /api/version returns consistent version string."""
    from apps.api.main import app
    from fastapi.testclient import TestClient

    client = TestClient(app)
    res = client.get("/api/version")
    assert res.status_code == 200
    data = res.json()
    assert data["version"] == "0.9.0"
    assert data["release_codename"] == "tournament-sandbox-v0.9.0"
