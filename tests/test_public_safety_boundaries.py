"""Adversarial regressions for the independently reproduced release blockers."""

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from mcp import Client

from apps.mcp_server import remote_server
from apps.mcp_server.protocol import CONFIRM_WRITES_ENV
from brain.autonomy.goal_run import GoalRun, GoalRunService
from brain.config.settings import settings
from brain.improvement.redaction import redact_trajectory_content
from brain.lab.engine import ValidationRunner
from brain.lab.models import CommandDef, ValidationProfile
from brain.lab import sandbox


@pytest.mark.parametrize("mode", ["auto", "docker", "unshare", "invalid", "off"])
def test_unavailable_or_unsafe_backend_never_executes_repository_code(tmp_path, monkeypatch, mode):
    monkeypatch.setattr(settings, "LAB_SANDBOX_MODE", mode)
    monkeypatch.setattr(settings, "LAB_ALLOW_UNSAFE_HOST_EXECUTION", False)
    monkeypatch.setattr(sandbox, "_docker_available", lambda: False)
    marker = tmp_path / "executed"
    (tmp_path / "compileall.py").write_text(f"open({str(marker)!r}, 'w').write('unsafe')")
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("must reject before spawn"))
    report = ValidationRunner.run_profile(tmp_path, ValidationProfile(
        profile_name="hostile", commands=[CommandDef(argv=["python", "-m", "compileall", "-q", "."])],
    ))
    assert not report.passed
    assert report.rejected_reasons
    assert not marker.exists()


def test_docker_plan_contains_only_workspace_bind(tmp_path, monkeypatch):
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path.parent / "sibling" / ".venv"))
    argv, handle = sandbox.wrap_docker(workspace=tmp_path, argv=["/shared/projects/brain/.venv/bin/python", "-c", "print(1)"], env={}, resolved_exe=Path("/shared/projects/brain/.venv/bin/python"))
    mounts = [argv[i + 1] for i, value in enumerate(argv) if value == "-v"]
    assert mounts == [f"{tmp_path}:{tmp_path}:rw"]
    assert argv[-3:] == ["python3", "-c", "print(1)"]
    assert "none" in argv and "--read-only" in argv
    assert handle.backend == "docker"


@pytest.mark.asyncio
async def test_cancelled_goal_stays_terminal_without_execution(monkeypatch):
    service = GoalRunService()
    run = GoalRun("cancelled", "synthetic")
    service.active_runs[run.goal_id] = run
    monkeypatch.setattr(service, "_run_subprocess_test", lambda *a: pytest.fail("no oracle execution"))
    assert service.cancel_goal(run.goal_id)
    assert (await service.execute_goal_run(run.goal_id)).status == "CANCELLED"
    assert run.commit_sha is None and not run.deployed and run.telemetry == []


@pytest.mark.parametrize("prefix", ["pbk_", "pbr_", "github_pat_", "gho_", "hf_", "AIza", "nvapi-", "sk-ant-"])
def test_native_and_provider_secrets_redacted_in_trajectory_and_lab(prefix):
    token = prefix + "synthetic" * 5
    for text in (token, f'{{"token": "{token}"}}', f"error: {token}"):
        assert token not in redact_trajectory_content(text)
        assert token not in ValidationRunner._redact(text)
    assert redact_trajectory_content("ordinary compile error in payments.py") == "ordinary compile error in payments.py"


@pytest.mark.parametrize("url,allowed", [
    ("http://localhost:8000", True), ("http://127.0.0.1:8000", True),
    ("http://[::1]:8000", True), ("https://example.invalid", True),
    ("http://localhost.example.invalid", False), ("http://127.0.0.1.example.invalid", False),
    ("http://192.0.2.1", False), ("https://user:password@example.invalid", False),
    ("http://localhost:99999", False), ("file:///etc/passwd", False),
])
def test_bridge_requires_parsed_loopback_for_plaintext(url, allowed):
    assert remote_server._is_safe_base_url(url) is allowed


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_confirmation_failure_never_dispatches_write(monkeypatch, mode):
    monkeypatch.setenv(CONFIRM_WRITES_ENV, "true")

    async def broken(*args):
        raise RuntimeError("synthetic prompt failure")

    post = AsyncMock(return_value={"id": 1})
    monkeypatch.setattr(remote_server, "_post", post)
    async with Client(remote_server.mcp, mode=mode, elicitation_callback=broken) as client:
        if mode == "auto":
            # Modern confirmation runs in the client before the retry reaches
            # the server. Its callback failure surfaces as a task-group error.
            with pytest.raises(Exception, match="unhandled errors in a TaskGroup"):
                await client.call_tool("record_rule", {"name": "No SQL", "repo_path": "/synthetic"})
        else:
            result = await client.call_tool("record_rule", {"name": "No SQL", "repo_path": "/synthetic"})
            assert "cancelled" in str(result)
    post.assert_not_awaited()


def test_compile_only_cannot_import_workspace_compileall(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "LAB_SANDBOX_MODE", "off")
    monkeypatch.setattr(settings, "LAB_ALLOW_UNSAFE_HOST_EXECUTION", True)
    marker = tmp_path / "shadow-executed"
    (tmp_path / "compileall.py").write_text(f"open({str(marker)!r}, 'w').write('unsafe')")
    report = ValidationRunner.run_profile(tmp_path, ValidationProfile(
        profile_name="compile-only", commands=[CommandDef(argv=["python", "-m", "compileall", "-q", "."])],
    ))
    assert report.passed
    assert not marker.exists()


def test_unfinished_autonomy_is_not_exposed_by_normal_api():
    from apps.api.main import app
    assert not any(getattr(route, "path", "").startswith("/autonomy") for route in app.routes)
