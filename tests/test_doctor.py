"""Tests for `brain doctor` — the single-user install preflight."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from typer.testing import CliRunner

import apps.cli.commands.doctor as doctor_module
from apps.cli.main import cli

runner = CliRunner()


def _healthy():
    return {
        "postgres": {"status": "healthy", "message": "ok"},
        "redis": {"status": "healthy", "message": "ok"},
        "neo4j": {"status": "healthy", "message": "ok"},
    }


def _settings(tmp_path, **overrides):
    base = SimpleNamespace(
        ENVIRONMENT="local",
        TARGET_REPO_PATH=str(tmp_path),
        CONTEXT_PACK_OUTPUT_DIR=str(tmp_path / "context_packs"),
        REPORT_OUTPUT_DIR=str(tmp_path / "reports"),
        DEFAULT_LLM_PROVIDER="mock",
        DEFAULT_EMBEDDING_PROVIDER="mock",
        PROJECT_BRAIN_API_KEY="test-key",
        OPENAI_API_KEY=None,
        ANTHROPIC_API_KEY=None,
        GOOGLE_API_KEY=None,
        NVIDIA_API_KEY=None,
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def _doctor_env(tmp_path, health=None, **overrides):
    return (
        patch.object(doctor_module, "settings", _settings(tmp_path, **overrides)),
        patch.object(
            doctor_module, "check_health", AsyncMock(return_value=health or _healthy())
        ),
    )


def test_doctor_all_green(tmp_path):
    settings_patch, health_patch = _doctor_env(tmp_path)
    with settings_patch, health_patch:
        result = runner.invoke(cli, ["doctor"])
    assert result.exit_code == 0
    assert "ready to index" in result.output
    assert "FAIL" not in result.output


def test_doctor_fails_closed_when_services_down(tmp_path):
    down = _healthy()
    down["neo4j"] = {"status": "unhealthy", "error": "connection refused"}
    settings_patch, health_patch = _doctor_env(tmp_path, health=down)
    with settings_patch, health_patch:
        result = runner.invoke(cli, ["doctor"])
    assert result.exit_code == 1
    assert "docker compose up -d postgres redis neo4j" in result.output


def test_doctor_flags_provider_without_key(tmp_path):
    settings_patch, health_patch = _doctor_env(tmp_path, DEFAULT_LLM_PROVIDER="nvidia")
    with settings_patch, health_patch:
        result = runner.invoke(cli, ["doctor"])
    assert result.exit_code == 1
    assert "NVIDIA_API_KEY is not set" in result.output


def test_doctor_flags_unknown_provider(tmp_path):
    settings_patch, health_patch = _doctor_env(tmp_path, DEFAULT_EMBEDDING_PROVIDER="acme")
    with settings_patch, health_patch:
        result = runner.invoke(cli, ["doctor"])
    assert result.exit_code == 1
    assert "unknown embedding provider 'acme'" in result.output


def test_doctor_production_requires_api_key(tmp_path):
    settings_patch, health_patch = _doctor_env(
        tmp_path, ENVIRONMENT="production", PROJECT_BRAIN_API_KEY=None
    )
    with settings_patch, health_patch:
        result = runner.invoke(cli, ["doctor"])
    assert result.exit_code == 1
    assert "PROJECT_BRAIN_API_KEY is unset" in result.output


def test_doctor_bad_target_repo_fails(tmp_path):
    settings_patch, health_patch = _doctor_env(
        tmp_path, TARGET_REPO_PATH=str(tmp_path / "missing-repo")
    )
    with settings_patch, health_patch:
        result = runner.invoke(cli, ["doctor"])
    assert result.exit_code == 1
    assert "not a directory" in result.output


def test_writable_probe_preserves_existing_files(tmp_path):
    existing = tmp_path / ".doctor-probe"
    existing.write_text("keep existing artifact")
    failures = []
    with (
        patch.object(doctor_module, "context_packs_dir", return_value=tmp_path),
        patch.object(doctor_module, "reports_dir", return_value=tmp_path),
    ):
        doctor_module._check_writable_dirs(failures)
    assert failures == []
    assert existing.read_text() == "keep existing artifact"
