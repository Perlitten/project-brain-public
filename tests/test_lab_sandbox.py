"""B4 — hostile-fixture tests for the lab execution sandbox.

Every test skips when no enforcement backend is available on the host (the
"unverified" fallback stays honest rather than pretending containment).
"""
import tempfile
import time
from pathlib import Path

import pytest

from brain.lab.engine import ValidationRunner
from brain.lab.models import CommandDef, ValidationProfile
from brain.lab.sandbox import (
    SANDBOX_DOCKER,
    SANDBOX_OFF,
    network_isolation_status,
    resolve_sandbox_backend,
)

BACKEND = resolve_sandbox_backend()
needs_sandbox = pytest.mark.skipif(
    BACKEND == SANDBOX_OFF, reason="no sandbox backend (docker/unshare) available"
)


def _env() -> dict:
    return ValidationRunner.build_environment({})


def _run(ws: Path, code: str, timeout: int = 30) -> object:
    return ValidationRunner._run_one(
        ws, ["python3", "-c", code], _env(), timeout, 0, True, None, BACKEND
    )


def test_backend_detection_reports_actual_mechanism():
    status = network_isolation_status(BACKEND)
    if BACKEND == SANDBOX_OFF:
        assert status == "unverified"
    else:
        assert status == f"enforced:{BACKEND}"


@needs_sandbox
def test_sandboxed_command_executes_in_workspace(tmp_path):
    (tmp_path / "marker.txt").write_text("ws-data")
    res = _run(
        tmp_path,
        "import os; print(open('marker.txt').read().strip()); "
        "open('written_in_ws.txt', 'w').write('ok')",
    )
    assert res.exit_code == 0, res.stderr_snippet
    assert "ws-data" in res.stdout_snippet
    assert (tmp_path / "written_in_ws.txt").read_text() == "ok"


@needs_sandbox
def test_network_egress_denied(tmp_path):
    res = _run(
        tmp_path,
        "import urllib.request; urllib.request.urlopen('http://example.com', timeout=3)",
        timeout=20,
    )
    assert res.exit_code != 0
    assert "Traceback" in res.stderr_snippet or res.stderr_snippet


@needs_sandbox
def test_home_secrets_not_visible(tmp_path):
    canary = Path.home() / "lab_sandbox_canary.txt"
    canary.write_text("s3cr3t")
    try:
        res = _run(
            tmp_path,
            "import os\n"
            f"p = {str(canary)!r}\n"
            "print('canary visible:', os.path.exists(p))\n"
            "try: print('read:', open(p).read())\n"
            "except Exception as e: print('read failed:', type(e).__name__)",
        )
    finally:
        canary.unlink(missing_ok=True)
    assert res.exit_code == 0, res.stderr_snippet
    assert "canary visible: False" in res.stdout_snippet
    assert "s3cr3t" not in res.stdout_snippet


@needs_sandbox
def test_write_escape_does_not_touch_host(tmp_path):
    escape_target = Path.home() / "lab_sandbox_escape.txt"
    escape_target.unlink(missing_ok=True)
    res = _run(
        tmp_path,
        "import os\n"
        f"target = {str(escape_target)!r}\n"
        "try:\n"
        "    open(target, 'w').write('escaped')\n"
        "    print('write ok')\n"
        "except Exception as e:\n"
        "    print('write failed:', type(e).__name__)",
    )
    assert res.exit_code == 0, res.stderr_snippet
    # Whether the inner write "succeeded" into the throwaway layer or failed,
    # the host filesystem must be untouched.
    assert not escape_target.exists()
    escape_target.unlink(missing_ok=True)


@needs_sandbox
def test_forked_children_die_with_sandbox(tmp_path):
    # Child process would write a sentinel after the parent exits; if the
    # namespace/container is really torn down, the sentinel never appears.
    sentinel = tmp_path / "child_survived.txt"
    res = _run(
        tmp_path,
        "import os, time\n"
        "if os.fork() == 0:\n"
        "    time.sleep(2)\n"
        f"    open({str(sentinel)!r}, 'w').write('survived')\n"
        "    os._exit(0)\n"
        "print('parent exiting')",
    )
    assert res.exit_code == 0, res.stderr_snippet
    time.sleep(3)
    assert not sentinel.exists()


@needs_sandbox
def test_run_profile_records_enforced_backend(tmp_path):
    profile = ValidationProfile(
        profile_name="sandbox-probe",
        timeout_seconds=60,
        commands=[CommandDef(argv=["python3", "-c", "print('hi')"], required=True)],
    )
    report = ValidationRunner.run_profile(tmp_path, profile)
    assert report.network_isolation == f"enforced:{BACKEND}"
    assert report.passed is True


def test_off_backend_reports_unverified_when_forced():
    # Backend forced off must never claim enforcement.
    res = ValidationRunner._run_one(
        Path(tempfile.mkdtemp()),
        ["python3", "-c", "print(1)"],
        _env(),
        30,
        0,
        True,
        None,
        SANDBOX_OFF,
    )
    assert res.exit_code == 0
    assert network_isolation_status(SANDBOX_OFF) == "unverified"


@pytest.mark.skipif(BACKEND != SANDBOX_DOCKER, reason="docker backend not active")
def test_docker_container_removed_after_run(tmp_path):
    import subprocess

    res = _run(tmp_path, "print('in container')")
    assert res.exit_code == 0, res.stderr_snippet
    out = subprocess.run(
        ["docker", "ps", "-a", "--filter", "name=brain-lab-", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert "brain-lab-" not in out.stdout
