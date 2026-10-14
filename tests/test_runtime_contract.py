"""Keep the source install, image and CI on the same reviewed runtimes."""

from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def test_locked_python_runtime_contract():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert project["project"]["requires-python"] == ">=3.12,<3.13"
    assert project["tool"]["ruff"]["target-version"] == "py312"
    assert "Python 3.12" in (ROOT / "scripts/bootstrap.sh").read_text()
    assert "--require-hashes -r requirements-dev.lock" in (ROOT / "scripts/bootstrap.sh").read_text()
    assert "--no-deps -e ." in (ROOT / "scripts/bootstrap.sh").read_text()

    dockerfile = (ROOT / "Dockerfile").read_text()
    assert dockerfile.count("FROM python:3.12-slim") == 2
    assert "--require-hashes -r requirements.lock" in dockerfile
    assert "--no-deps ." in dockerfile
    assert "/usr/local/lib/python3.12/site-packages" in dockerfile
    assert "!requirements.lock" in (ROOT / ".dockerignore").read_text()
    assert "--include requirements.lock" in (ROOT / "deploy/server_up.sh").read_text()


def test_web_image_uses_ci_node_major():
    dockerfile = (ROOT / "apps/web/Dockerfile").read_text()
    assert dockerfile.count("FROM node:22-alpine") == 2
    assert "node-version: '22'" in (ROOT / ".github/workflows/ci.yml").read_text()
