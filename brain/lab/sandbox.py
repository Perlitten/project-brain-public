"""Fail-closed Docker isolation for repository-controlled validation.

Only the workspace is bound into the image. Toolchains must be installed in
that image: host PATH, virtualenvs and their parent directories are never
mounted. Unshare is rejected because it does not contain the host filesystem.
Host execution is exclusively an explicit trusted-development opt-in.
"""
from __future__ import annotations

import os
import json
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


class CandidateMetadataError(ValueError):
    """The candidate's project metadata is invalid, rather than its toolchain."""

def validate_image_toolchain(workspace: Path, commands: list, timeout: float) -> bool:
    """Probe image-owned tools/dependencies without importing repository code."""
    executables, modules = set(), set()
    for command in commands:
        if not command.required:
            continue
        name = command.argv[0].lower().removesuffix(".exe")
        executables.add("python3" if name in {"python", "python3", "py"} else name)
        if name in {"python", "python3", "py"} and "-m" in command.argv:
            index = command.argv.index("-m")
            if index + 1 < len(command.argv) and command.argv[index + 1] in {"pytest", "compileall", "ruff", "mypy"}:
                modules.add(command.argv[index + 1])
    # -I keeps the mounted repository out of import resolution. Metadata reads
    # installed distributions; it does not import their or the project's code.
    probe = """import importlib.util, importlib.metadata, json, shutil, sys
from pathlib import Path
config = json.loads(sys.argv[1])
executables, modules = config['executables'], config['modules']
ok = all(shutil.which(name) for name in executables)
ok = ok and all(importlib.util.find_spec(name.split('.')[0]) for name in modules)
requirements = []
if config['test_dependencies'] and Path(config['project']).is_file():
    try:
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib
        from packaging.requirements import Requirement
    except ImportError:
        sys.exit(1)
    try:
        project = tomllib.loads(Path(config['project']).read_text(encoding='utf-8')).get('project', {})
        if not isinstance(project, dict) or not isinstance(project.get('dependencies', []), list):
            sys.exit(2)
        requirements.extend(project.get('dependencies', []))
        extras = project.get('optional-dependencies', {})
        if not isinstance(extras, dict):
            sys.exit(2)
        for group in ('dev', 'test', 'tests'):
            if not isinstance(extras.get(group, []), list):
                sys.exit(2)
            requirements.extend(extras.get(group, []))
    except (OSError, ValueError, TypeError, AttributeError):
        sys.exit(2)
for specification in requirements:
    try:
        requirement = Requirement(specification)
        if requirement.marker and not requirement.marker.evaluate():
                continue
        version = importlib.metadata.version(requirement.name)
        ok = ok and version in requirement.specifier
    except (ValueError, TypeError):
        sys.exit(2)
    except (ImportError, importlib.metadata.PackageNotFoundError):
        ok = False
sys.exit(0 if ok else 1)
"""
    config = {"executables": sorted(executables), "modules": sorted(modules),
              "project": str(workspace.resolve() / "pyproject.toml"),
              "test_dependencies": "pytest" in executables or "pytest" in modules}
    argv, handle = wrap_docker(workspace=workspace, argv=["python3", "-I", "-c", probe,
                               json.dumps(config)],
                               env={}, resolved_exe=Path("python3"))
    try:
        result = subprocess.run(argv, capture_output=True, timeout=max(.1, timeout))
        if result.returncode == 2:
            raise CandidateMetadataError("Candidate pyproject.toml contains invalid project metadata")
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        handle.terminate()
        return False


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
        if key == "PATH":
            continue
        cmd += ["-e", f"{key}={value}"]
    cmd += ["-v", f"{ws}:{ws}:rw"]
    # Resolve the tool inside the trusted image, never through host mounts.
    executable = Path(argv[0]).name.lower().removesuffix(".exe")
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
