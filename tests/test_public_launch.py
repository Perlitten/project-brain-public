"""Public handoffs, release labels and real Compose environment interpolation."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tomllib

import pytest

from brain import __version__
from scripts.check_public_launch import obsolete_references
from scripts.check_docs_consistency import documentation_findings


ROOT = Path(__file__).resolve().parents[1]


def test_public_identity_guard_detects_old_links_but_keeps_local_names(tmp_path):
    legacy = "Perlitten/" + "project-brain"
    (tmp_path / "README.md").write_text(f"https://github.com/{legacy}/issues\nlocal: /opt/project-brain\n")
    assert obsolete_references(tmp_path, ["README.md"]) == ["README.md:1: obsolete public repository identity"]
    (tmp_path / "README.md").write_text(f"https://github.com/{legacy}-public\nlocal: /opt/project-brain\n")
    assert not obsolete_references(tmp_path, ["README.md"])


def test_package_docs_agree_on_published_release_and_source_contracts():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["version"] == __version__
    assert documentation_findings(ROOT) == []


def _compose(tmp_path, **values):
    if not shutil.which("docker"):
        pytest.skip("Docker Compose is unavailable")
    env = {k: v for k, v in os.environ.items() if k not in {
        "POSTGRES_PASSWORD", "NEO4J_PASSWORD", "PROJECT_BRAIN_API_KEY", "BRAIN_IMAGE_TAG", "WEB_BASIC_AUTH",
        "COMPOSE_FILE", "COMPOSE_PROJECT_NAME",
    }}
    env.update(values)
    return subprocess.run(
        ["docker", "compose", "--env-file", "/dev/null", "--project-directory", str(tmp_path),
         "-f", str(ROOT / "docker-compose.quickstart.yml"), "config", "--format", "json"],
        capture_output=True, text=True, env=env,
    )


def test_experimental_images_require_explicit_tag_and_credentials(tmp_path):
    result = _compose(tmp_path)
    assert result.returncode != 0
    assert "required variable" in result.stderr


def test_quickstart_propagates_credentials_and_waits_for_storage(tmp_path):
    result = _compose(tmp_path, POSTGRES_PASSWORD="test-db-password", NEO4J_PASSWORD="test-graph-password",
                      PROJECT_BRAIN_API_KEY="test-only-api-key", WEB_BASIC_AUTH="test:test-password",
                      BRAIN_IMAGE_TAG="local-test")
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    # Neo4j interprets NEO4J_* as configuration keys; a convenience PASSWORD
    # variable breaks its entrypoint. Only its supported AUTH binding goes there.
    assert "NEO4J_PASSWORD" not in services["neo4j"]["environment"]
    for name in ("api", "worker"):
        env = services[name]["environment"]
        assert env["NEO4J_USER"] == "neo4j"
        assert env["NEO4J_PASSWORD"] == services["neo4j"]["environment"]["NEO4J_AUTH"].split("/", 1)[1]
        assert "NEO4J_AUTH" not in env
        assert env["DEFAULT_LLM_PROVIDER"] == env["DEFAULT_EMBEDDING_PROVIDER"] == "mock"
        assert env["PROJECT_BRAIN_API_KEY"] == services["web"]["environment"]["BRAIN_API_KEY"]
    assert all(services["api"]["depends_on"][name]["condition"] == "service_healthy"
               for name in ("postgres", "redis", "neo4j"))
    assert services["worker"]["depends_on"]["api"]["condition"] == "service_healthy"
    assert all(services[name]["ports"][0]["host_ip"] == "127.0.0.1" for name in ("api", "web"))
