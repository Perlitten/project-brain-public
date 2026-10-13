"""Package metadata and API must agree on the release identity."""

from pathlib import Path
import tomllib

from brain._version import RELEASE_CODENAME, __version__


def test_release_identity_consistency():
    """Verify package version, brain._version, and pyproject.toml match."""
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())["project"]
    assert __version__ == project["version"]
    assert __version__ in RELEASE_CODENAME


def test_api_version_endpoint():
    """Verify /api/version returns consistent version string."""
    from apps.api.main import app
    from fastapi.testclient import TestClient

    client = TestClient(app)
    res = client.get("/api/version")
    assert res.status_code == 200
    data = res.json()
    assert data["version"] == __version__
    assert data["release_codename"] == RELEASE_CODENAME
