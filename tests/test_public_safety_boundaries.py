"""Adversarial regressions for the independently reproduced release blockers."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from mcp import Client

from apps.mcp_server import protocol, remote_server
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


@pytest.mark.parametrize("scheme", ["Basic", "Digest", "Custom"])
def test_non_bearer_authorization_redacts_whole_credential(scheme):
    credential = "synthetic-authorization-value"
    for raw in (f"Authorization: {scheme} {credential}", f'{{"authorization": "{scheme} {credential}"}}'):
        assert credential not in redact_trajectory_content(raw)
        assert credential not in ValidationRunner._redact(raw)


def test_complete_and_truncated_private_key_blocks_are_redacted():
    opening = "-----BEGIN " + "PRIVATE KEY-----"
    closing = "-----END " + "PRIVATE KEY-----"
    text = "safe preceding log\n" + opening + "\n" + "synthetic-key-material" * 500 + "\n" + closing
    for raw in (text, text[:ValidationRunner.MAX_OUTPUT_CHARS]):
        assert "synthetic-key-material" not in redact_trajectory_content(raw)
        assert "synthetic-key-material" not in ValidationRunner._redact(raw)


def test_container_tool_does_not_require_host_path(tmp_path, monkeypatch):
    monkeypatch.setattr(ValidationRunner, "_resolve_executable", lambda *a: pytest.fail("host PATH must not be consulted"))
    process = Mock()
    process.communicate.return_value = ("image-owned-tool", "")
    process.returncode = 0
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: process)
    result = ValidationRunner._run_one(tmp_path, ["ruff", "check", "."], {}, 5, 0, True, None, "docker")
    assert result.exit_code == 0
    assert result.stdout_snippet == "image-owned-tool"


def test_incompatible_image_is_infrastructure_error_before_any_candidate_command(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "LAB_SANDBOX_MODE", "docker")
    monkeypatch.setattr(sandbox, "_docker_available", lambda: True)
    monkeypatch.setattr(sandbox, "validate_image_toolchain", lambda *a: False)
    monkeypatch.setattr(ValidationRunner, "_run_one", lambda *a: pytest.fail("candidate must not run"))
    report = ValidationRunner.run_profile(tmp_path, ValidationProfile(
        profile_name="python-standard", commands=[CommandDef(argv=["python", "-m", "pytest", "-q"])],
    ))
    assert report.infrastructure_error and not report.passed
    assert report.results == [] and report.rejected_reasons


def test_optional_tool_does_not_block_required_image_preflight(tmp_path, monkeypatch):
    captured = {}

    def probe(argv, **kwargs):
        captured["tools"] = json.loads(argv[-1])
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", probe)
    commands = [CommandDef(argv=["python", "-m", "compileall", "."]),
                CommandDef(argv=["ruff", "check", "."], required=False)]
    assert sandbox.validate_image_toolchain(tmp_path, commands, 5)
    assert captured["tools"]["executables"] == ["python3"]
    assert captured["tools"]["modules"] == ["compileall"]
    assert captured["tools"]["test_dependencies"] is False


def test_dependency_markers_are_evaluated_by_image_interpreter(tmp_path, monkeypatch):
    # Force a different target environment in the probe, without downloading
    # an image or changing the host interpreter. A host-side evaluation would
    # wrongly retain this requirement before that probe sees it.
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = ["not-installed-in-this-fixture; python_version >= \'3.12\'"]\n',
        encoding="utf-8",
    )

    def run_probe(argv, **kwargs):
        probe, payload = argv[-2:]
        assert json.loads(payload)["project"] == str(tmp_path / "pyproject.toml")
        target_environment = (
            "import packaging.markers\n"
            "original = packaging.markers.default_environment\n"
            "packaging.markers.default_environment = lambda: dict(original(), python_version='3.11')\n"
        )
        return original_run([sys.executable, "-I", "-c", target_environment + probe, payload], **kwargs)

    original_run = subprocess.run
    monkeypatch.setattr(subprocess, "run", run_probe)
    assert sandbox.validate_image_toolchain(tmp_path, [CommandDef(argv=["python", "-m", "pytest"])], 5)


@pytest.mark.parametrize("alias", ["PYTHON", "python.exe", "py.exe", "PYTHON3"])
def test_accepted_python_aliases_execute_image_python(tmp_path, alias):
    argv, _ = sandbox.wrap_docker(workspace=tmp_path, argv=[alias, "-V"], env={}, resolved_exe=Path(alias))
    assert argv[-2:] == ["python3", "-V"]


def test_pytest_marker_named_compileall_is_not_rewritten(tmp_path, monkeypatch):
    captured = {}
    process = Mock()
    process.communicate.return_value = ("", "")
    process.returncode = 0

    def spawn(argv, **kwargs):
        captured["argv"] = argv
        return process

    monkeypatch.setattr(subprocess, "Popen", spawn)
    result = ValidationRunner._run_one(tmp_path, ["pytest", "-m", "compileall"], {}, 5, 0, True, None, "docker")
    assert result.exit_code == 0
    assert captured["argv"][-3:] == ["pytest", "-m", "compileall"]


def test_disabled_goal_cli_reports_blocked_without_execution(monkeypatch, capsys):
    from brain.autonomy import cli
    monkeypatch.setattr(sys, "argv", ["brain.autonomy", "submit-goal", "--goal-id", "fixture", "--description", "fixture"])
    monkeypatch.setattr(GoalRunService, "execute_goal_run", lambda *a: pytest.fail("blocked goal must not execute"))
    assert cli.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "BLOCKED"
    assert "commit_sha" not in result


def test_disabled_goal_constructor_does_not_create_state(monkeypatch):
    monkeypatch.setattr(Path, "mkdir", lambda *a, **k: pytest.fail("disabled service must not create state"))
    assert GoalRunService().workspace_mgr is None


def test_redaction_preserves_json_structure_and_benign_evidence():
    original = {"output": "Authorization: Basic synthetic-private-value", "tests_passed": 17,
                "nested": {"authorization": "Custom synthetic-private-value", "path": "brain/core.py"}}
    for redactor in (redact_trajectory_content, ValidationRunner._redact):
        result = redactor(json.dumps(original))
        decoded = json.loads(result)
        assert "synthetic-private-value" not in result
        assert decoded["tests_passed"] == 17
        assert decoded["nested"]["path"] == "brain/core.py"


@pytest.mark.parametrize("key", ["apiKey", "xApiKey", "AWS_SECRET_ACCESS_KEY", "privateKey", "refreshToken"])
def test_common_credential_fields_redact_without_provider_prefix(key):
    for redactor in (redact_trajectory_content, ValidationRunner._redact):
        for raw in (json.dumps({key: "synthetic-private-value", "tests_passed": 17}), f"{key}=synthetic-private-value"):
            result = redactor(raw)
            assert "synthetic-private-value" not in result


def test_lab_json_assignment_preserves_adjacent_evidence():
    result = json.loads(ValidationRunner._redact('{"output":"TOKEN=abc","tests_passed":17}'))
    assert result["tests_passed"] == 17
    assert "abc" not in result["output"]


def test_preflight_checks_test_extras_without_host_metadata_imports(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies=[]\n[project.optional-dependencies]\ndev=["fixture-missing-test-plugin"]\n', encoding="utf-8",
    )
    original_run = subprocess.run

    def run_probe(argv, **kwargs):
        probe, payload = argv[-2:]
        return original_run([sys.executable, "-I", "-c", probe, payload], **kwargs)

    import builtins
    original_import = builtins.__import__

    def reject_host_metadata(name, *args, **kwargs):
        if name in {"tomllib", "tomli", "packaging.requirements"}:
            pytest.fail("project metadata must be parsed only in the image")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_host_metadata)
    monkeypatch.setattr(subprocess, "run", run_probe)
    assert not sandbox.validate_image_toolchain(tmp_path, [CommandDef(argv=["python", "-m", "pytest"])], 5)


def test_candidate_module_is_not_an_image_owned_tool(tmp_path, monkeypatch):
    (tmp_path / "fixture_check.py").write_text("print('candidate module')\n", encoding="utf-8")
    captured = {}

    def run_probe(argv, **kwargs):
        captured["config"] = json.loads(argv[-1])
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", run_probe)
    assert sandbox.validate_image_toolchain(tmp_path, [CommandDef(argv=["python", "-m", "fixture_check"])], 5)
    assert captured["config"]["modules"] == []


@pytest.mark.asyncio
async def test_legacy_confirmation_timeout_cancels_write(monkeypatch):
    monkeypatch.setenv(CONFIRM_WRITES_ENV, "true")
    monkeypatch.setattr(protocol, "CONFIRMATION_TIMEOUT_SECONDS", .02)
    cancelled = asyncio.Event()

    async def never_responds(*args):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    post = AsyncMock()
    monkeypatch.setattr(remote_server, "_post", post)
    async with Client(remote_server.mcp, mode="legacy", elicitation_callback=never_responds) as client:
        result = await asyncio.wait_for(client.call_tool("record_rule", {"name": "fixture", "repo_path": "/fixture"}), 2)
        assert "cancelled" in str(result)
    post.assert_not_awaited()
    assert cancelled.is_set()
