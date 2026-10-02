"""OS-level sandboxing for lab validation commands.

Two backends, probed once per run:

* ``docker`` — disposable container: ``--network none``, ``--read-only``,
  ``--cap-drop ALL``, ``no-new-privileges``, numeric ``--pids-limit`` /
  ``--memory`` / ``--cpus``, the workspace is the only writable host path and
  only allowlisted toolchain paths are mounted read-only. Preferred when the
  daemon and the configured image are both available.
* ``unshare`` — unprivileged Linux namespaces: user (root mapped to the real
  uid), network (empty namespace, no egress), PID + ``--fork`` + ``--kill-child``
  (tree dies with the namespace init, so group-kill semantics stay airtight),
  private ``/proc``, and a mount namespace that hides ``$HOME`` behind a tmpfs
  and re-exposes only the workspace plus allowlisted toolchain paths.

Both keep the existing supervision contract: argv is never shell-joined
(argv[0] still resolved from the allowlist), env is the allowlisted dict, and
``terminate()`` kills the whole sandbox (``docker rm -f`` or the namespace
init's process group).

Residuals by backend: ``unshare`` has no numeric PID/memory cap — the kernel
has no unprivileged cgroup here — so a fork bomb is contained only by the
CPU/time limits plus namespace teardown; ``docker`` enforces numeric caps.
World-readable files outside ``$HOME`` stay visible under both (only secret-
bearing user state is hidden); full rootfs isolation needs the docker backend.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
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


def _home() -> Optional[Path]:
    home = os.environ.get("HOME")
    if not home:
        return None
    try:
        return Path(home).resolve()
    except OSError:
        return None


def _top_anchor_under_home(path: Path, home: Path) -> Optional[Path]:
    """Return the first path component under ``home`` for ``path``.

    ``/home/u/.pyenv/shims/python`` → ``/home/u/.pyenv``. Hiding all of
    ``$HOME`` then re-exposing per-file would need a tree walk; anchoring at
    the top-level directory keeps the mount list small and predictable.
    """
    try:
        rel = Path(path).resolve().relative_to(home)
    except (ValueError, OSError):
        return None
    return home / rel.parts[0] if rel.parts else None


def keep_mounts(workspace: Path, resolved_exe: Optional[Path]) -> List[Path]:
    """Host paths that must stay reachable inside the sandbox.

    The workspace (writable) plus every toolchain path under ``$HOME`` that the
    command or interpreter depends on, plus operator-configured extras.
    """
    home = _home()
    out: List[Path] = []

    def add(path: Optional[Path]) -> None:
        if path is None:
            return
        try:
            resolved = path.resolve()
        except OSError:
            return
        if not resolved.exists() or resolved in out or resolved == workspace:
            return
        out.append(resolved)

    if home is not None:
        for exe_path in (resolved_exe, Path(sys.executable)):
            add(_top_anchor_under_home(exe_path, home) if exe_path else None)
        for prefix in (sys.prefix, sys.base_prefix, os.environ.get("VIRTUAL_ENV")):
            if prefix:
                add(_top_anchor_under_home(Path(prefix), home))
        for entry in os.environ.get("PATH", "").split(os.pathsep):
            if entry:
                add(_top_anchor_under_home(Path(entry), home))

    for extra in settings.LAB_SANDBOX_EXTRA_MOUNTS:
        add(Path(extra))
    return out


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


def _unshare_available() -> bool:
    unshare = shutil.which("unshare")
    if unshare is None or os.name == "nt":
        return False
    try:
        probe = subprocess.run(
            [
                unshare,
                "--user",
                "--map-root-user",
                "--net",
                "--pid",
                "--fork",
                "--mount-proc",
                "true",
            ],
            capture_output=True,
            timeout=_PROBE_TIMEOUT,
        )
        return probe.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def resolve_sandbox_backend() -> str:
    """Pick the enforcement backend for this run; ``off`` when nothing fits."""
    mode = (settings.LAB_SANDBOX_MODE or "auto").lower()
    if mode == SANDBOX_OFF:
        return SANDBOX_OFF
    candidates = {
        SANDBOX_DOCKER: _docker_available,
        SANDBOX_UNSHARE: _unshare_available,
    }
    if mode == "auto":
        order: tuple[str, ...] = (SANDBOX_DOCKER, SANDBOX_UNSHARE)
    elif mode in candidates:
        order = (mode,)
    else:
        return SANDBOX_OFF
    for name in order:
        if candidates[name]():
            return name
    return SANDBOX_OFF


def network_isolation_status(backend: str) -> str:
    return f"enforced:{backend}" if backend in (SANDBOX_DOCKER, SANDBOX_UNSHARE) else "unverified"


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


def wrap_unshare(
    *,
    workspace: Path,
    argv: List[str],
    resolved_exe: Path,
) -> tuple[List[str], SandboxHandle]:
    """Prepend the namespace + mount-namespace wrapper to ``argv``.

    Returns the wrapped argv and a handle whose ``terminate`` kills the
    namespace init's process group (normal group-kill applies — the handle is
    a no-op there because ``--kill-child`` already guarantees teardown).
    """
    unshare = shutil.which("unshare") or "unshare"
    bash = shutil.which("bash") or "bash"
    ws = workspace.resolve()
    keeps = [ws, *keep_mounts(ws, resolved_exe)]
    home = _home()

    # mount namespaces resolve bind sources at mount time: stage each keep path
    # under /tmp before covering $HOME, then rebind them at their real paths.
    prelude_parts = [
        "set -e",
        "mount --make-rprivate /",
    ]
    if home is not None and keeps:
        stage = "/tmp/.lab-keep"
        prelude_parts.append(f"mkdir -p {shlex.quote(stage)}")
        for idx, keep in enumerate(keeps):
            prelude_parts.append(f"mkdir -p {shlex.quote(f'{stage}/{idx}')}")
            prelude_parts.append(
                f"mount --bind {shlex.quote(str(keep))} {shlex.quote(f'{stage}/{idx}')}"
            )
        prelude_parts.append(f"mount -t tmpfs -o size=4m tmpfs {shlex.quote(str(home))}")
        for idx, keep in enumerate(keeps):
            prelude_parts.append(f"mkdir -p {shlex.quote(str(keep))}")
            prelude_parts.append(
                f"mount --bind {shlex.quote(f'{stage}/{idx}')} {shlex.quote(str(keep))}"
            )
    prelude_parts.append('exec "$@"')
    prelude = "; ".join(prelude_parts)

    wrapped = [
        unshare,
        "--user",
        "--map-root-user",
        "--net",
        "--pid",
        "--fork",
        "--kill-child",
        "--mount",
        "--mount-proc",
        bash,
        "-c",
        prelude,
        "brain-lab-sandbox",
        *argv,
    ]
    return wrapped, SandboxHandle(SANDBOX_UNSHARE)


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
        "/tmp:rw,nosuid,size=64m",
        "-w",
        str(ws),
    ]
    for key, value in env.items():
        cmd += ["-e", f"{key}={value}"]
    cmd += ["-v", f"{ws}:{ws}:rw"]
    for keep in keep_mounts(ws, resolved_exe):
        cmd += ["-v", f"{keep}:{keep}:ro"]
    for sysdir in _SYSTEM_TOOLCHAIN_MOUNTS:
        if Path(sysdir).is_dir():
            cmd += ["-v", f"{sysdir}:{sysdir}:ro"]
    cmd += [settings.LAB_SANDBOX_IMAGE, *argv]

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
    if backend == SANDBOX_UNSHARE:
        return wrap_unshare(workspace=workspace, argv=argv, resolved_exe=resolved_exe)
    return list(argv), SandboxHandle(SANDBOX_OFF)
