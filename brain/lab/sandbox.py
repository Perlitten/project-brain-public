"""Fail-closed Docker isolation for repository-controlled validation.

Only the workspace is bound into the image. Toolchains must be installed in
that image: host PATH, virtualenvs and their parent directories are never
mounted. Unshare is rejected because it does not contain the host filesystem.
Host execution is exclusively an explicit trusted-development opt-in.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Callable, Dict, List, Optional

from brain.config.settings import settings


SANDBOX_DOCKER = "docker"
SANDBOX_UNSHARE = "unshare"
SANDBOX_OFF = "off"

_PROBE_TIMEOUT = 10

# Host directories mounted read-only into the sandbox so allowlisted
# executables resolve identically inside it.
_SYSTEM_TOOLCHAIN_MOUNTS = ("/usr", "/bin", "/sbin", "/lib", "/lib64")


def _docker_available() -> bool:
    docker = shutil.which("docker")
    if docker is None:
        return False
    try:
        info = subprocess.run(
            [docker, "info"], capture_output=True, timeout=_PROBE_TIMEOUT
        )
        if info.returncode != 0:
            return False
        image = subprocess.run(
            [docker, "images", "-q", settings.LAB_SANDBOX_IMAGE],
            capture_output=True,
            timeout=_PROBE_TIMEOUT,
        )
        return bool(image.stdout.strip())
    except (subprocess.TimeoutExpired, OSError):
        return False


def resolve_sandbox_backend() -> str:
    """Return unavailable instead of silently executing on the host."""
    mode = (settings.LAB_SANDBOX_MODE or "auto").lower()
    if mode == SANDBOX_OFF and settings.LAB_ALLOW_UNSAFE_HOST_EXECUTION:
        return SANDBOX_OFF
    if mode in ("auto", SANDBOX_DOCKER) and _docker_available():
        return SANDBOX_DOCKER
    return "unavailable"


def network_isolation_status(backend: str) -> str:
    return f"enforced:{backend}" if backend == SANDBOX_DOCKER else "unverified"


class SandboxHandle:
    """Per-command sandbox instance; ``terminate`` is backend-specific."""

    def __init__(self, backend: str, terminate: Optional[Callable[[], None]] = None):
        self.backend = backend
        self._terminate = terminate

    def terminate(self) -> None:
        if self._terminate is not None:
            try:
                self._terminate()
            except Exception:
                pass


def wrap_docker(
    *,
    workspace: Path,
    argv: List[str],
    env: Dict[str, str],
    resolved_exe: Path,
) -> tuple[List[str], SandboxHandle]:
    """Wrap ``argv`` in a disposable, network-less, capped container."""
    docker = shutil.which("docker") or "docker"
    ws = workspace.resolve()
    name = f"brain-lab-{uuid.uuid4().hex[:12]}"

    cmd: List[str] = [
        docker,
        "run",
        "--rm",
        "--name",
        name,
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--pids-limit",
        str(settings.LAB_SANDBOX_PIDS_LIMIT),
        "--memory",
        f"{settings.LAB_SANDBOX_MEMORY_MB}m",
        "--memory-swap",
        f"{settings.LAB_SANDBOX_MEMORY_MB}m",
        "--cpus",
        str(settings.LAB_SANDBOX_CPUS),
        "--tmpfs",
        "/tmp:rw,nosuid,size=64m",  # nosec B108
        "-w",
        str(ws),
    ]
    for key, value in env.items():
        cmd += ["-e", f"{key}={value}"]
    cmd += ["-v", f"{ws}:{ws}:rw"]
    # Resolve the tool inside the trusted image, never through host mounts.
    executable = Path(argv[0]).name
    if executable in {"python", "python3", "py"}:
        executable = "python3"
    cmd += [settings.LAB_SANDBOX_IMAGE, executable, *argv[1:]]

    def _terminate() -> None:
        subprocess.run(
            [docker, "rm", "-f", name], capture_output=True, timeout=30
        )

    return cmd, SandboxHandle(SANDBOX_DOCKER, _terminate)


def wrap_argv(
    backend: str,
    *,
    workspace: Path,
    argv: List[str],
    env: Dict[str, str],
    resolved_exe: Path,
) -> tuple[List[str], SandboxHandle]:
    if backend == SANDBOX_DOCKER:
        return wrap_docker(workspace=workspace, argv=argv, env=env, resolved_exe=resolved_exe)
    if backend == SANDBOX_OFF and settings.LAB_ALLOW_UNSAFE_HOST_EXECUTION:
        return list(argv), SandboxHandle(SANDBOX_OFF)
    raise PermissionError("Validation requires an available Docker sandbox; host execution is disabled.")
