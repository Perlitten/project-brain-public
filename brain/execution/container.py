"""Container backend for bounded team execution (Team M2).

``ContainerBackend`` keeps the subprocess backend's clone-per-run working copy
and evidence format but runs every step in a fresh, hardened container through
the docker CLI:

- ``--network none`` unless the run holds an egress allowlist; then a per-run
  ``--internal`` network whose only other member is the allowlist proxy
  (``brain.execution.egress``) started as a sidecar;
- ``--read-only`` root, a size-capped ``/tmp`` tmpfs, the working copy bind
  mounted at ``/workspace`` and nothing else from the host;
- secrets as read-only files under ``/run/brain-secrets`` (never environment
  variables or argv), written per step and removed after it;
- a non-root ``--user``, ``--cap-drop ALL``, ``no-new-privileges``, cpu,
  memory and pids limits, no log driver (output is captured and redacted here);
- the image pinned by ID (``docker image inspect``) at the start of the run and
  never pulled implicitly (``--pull never``);
- the container is killed on timeout or a failed revocation checkpoint.

The head of the working copy is read by git *inside* a hardened container:
after a step has run, the repository's ``.git`` is untrusted (``core.fsmonitor``
or filter drivers would execute on the host).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from brain.execution.bounded import (_GIT_TIMEOUT_SECONDS, CHANGED_PATHS_GIT, PATCH_GIT, SHA_PATTERN, ExecutorError,
                                     StepOutcome, StepRequest, StopProbe, SubprocessBackend, WorkingCopy, build_patch,
                                     finish_step, parse_changed_paths, supervise)
from brain.execution.egress import PROXY_ALIAS, PROXY_PORT, READY_EVENT, EgressAllowlist

WORKSPACE = "/workspace"
SECRETS_TARGET = "/run/brain-secrets"
RUN_LABEL = "brain.execution.run"
_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_CPUS = re.compile(r"^[0-9]+(\.[0-9]+)?$")
_MEMORY = re.compile(r"^[0-9]+[bkmg]?$")
_USER = re.compile(r"^[0-9]+(:[0-9]+)?$")
_NETWORK = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
_IMAGE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/:@-]{0,254}$")
_DOCKER_TIMEOUT = 60
_PROXY_READY_SECONDS = 20.0
_UNSAFE_MOUNT_CHARS = set(",\"'\n\r=")
_DOCKER_ENV = ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY", "HOME",
               "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
               "PROGRAMDATA", "TEMP", "TMP")


def valid_cpus(value: str) -> bool:
    return bool(_CPUS.match(value)) and float(value) > 0


def valid_memory(value: str) -> bool:
    return bool(_MEMORY.match(value.lower())) and int(value.lower().rstrip("bkmg")) > 0


def valid_user(value: str) -> bool:
    """``uid[:gid]`` in numbers; root (uid 0 or gid 0) is refused."""
    if not _USER.match(value):
        return False
    return all(int(part) != 0 for part in value.split(":"))


def valid_image(value: str) -> bool:
    return bool(_IMAGE.match(value))


def valid_network(value: str) -> bool:
    return bool(_NETWORK.match(value))


def default_user() -> str:
    """The server's own uid:gid (so the working copy and secret files stay accessible), never root."""
    getuid, getgid = getattr(os, "getuid", None), getattr(os, "getgid", None)
    if getuid is not None and getgid is not None and getuid() != 0 and getgid() != 0:
        return f"{getuid()}:{getgid()}"
    return "65534:65534"


@dataclass(frozen=True)
class Environment:
    """An operator-defined execution environment: an image and the tools allowed in it."""
    name: str
    image: str
    tools: frozenset[str]
    cpus: str | None = None
    memory: str | None = None
    pids: int | None = None


@dataclass(frozen=True)
class ResolvedEnvironment:
    environment: Environment
    image_id: str
    repo_digests: tuple[str, ...] = ()

    def evidence(self) -> dict:
        return {"name": self.environment.name, "image": self.environment.image, "image_id": self.image_id,
                "repo_digests": list(self.repo_digests)}


@dataclass(frozen=True)
class ContainerSettings:
    docker: tuple[str, ...] = ("docker",)
    user: str = field(default_factory=default_user)
    cpus: str = "1"
    memory: str = "1g"
    pids: int = 256
    tmpfs_size: str = "256m"
    proxy_image: str = ""
    upstream_network: str = "bridge"


@dataclass(frozen=True)
class StepContainer:
    """Everything that shapes one step's ``docker run``; ``step_argv`` turns it into argv."""
    name: str
    run_id: str
    image_id: str
    workdir: Path
    user: str
    cpus: str
    memory: str
    pids: int
    tmpfs_size: str
    command: tuple[str, ...]
    network: str | None = None
    secrets_dir: Path | None = None


def _hardening(user: str, run_id: str, tmpfs_size: str) -> list[str]:
    return ["--pull", "never", "--read-only", "--tmpfs", f"/tmp:rw,nosuid,nodev,size={tmpfs_size}",
            "--user", user, "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--label", f"{RUN_LABEL}={run_id}", "--env", "HOME=/tmp", "--env", "TMPDIR=/tmp",
            "--env", "LANG=C.UTF-8", "--env", "PYTHONDONTWRITEBYTECODE=1"]


def step_argv(docker: Sequence[str], spec: StepContainer) -> list[str]:
    """The exact ``docker run`` argv for one step (pure; tests assert it)."""
    argv = [*docker, "run", "--rm", "--init", "--name", spec.name, "--network", spec.network or "none",
            *_hardening(spec.user, spec.run_id, spec.tmpfs_size),
            "--mount", f"type=bind,source={spec.workdir},target={WORKSPACE}", "--workdir", WORKSPACE,
            "--cpus", spec.cpus, "--memory", spec.memory, "--memory-swap", spec.memory,
            "--pids-limit", str(spec.pids), "--log-driver", "none",
            "--env", "GIT_CONFIG_NOSYSTEM=1", "--env", "GIT_TERMINAL_PROMPT=0"]
    if spec.secrets_dir is not None:
        argv += ["--mount", f"type=bind,source={spec.secrets_dir},target={SECRETS_TARGET},readonly",
                 "--env", f"BRAIN_SECRETS_DIR={SECRETS_TARGET}"]
    if spec.network is not None:
        proxy = f"http://{PROXY_ALIAS}:{PROXY_PORT}"
        for variable in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            argv += ["--env", f"{variable}={proxy}"]
        argv += ["--env", "NO_PROXY=", "--env", "no_proxy="]
    return [*argv, "--entrypoint", spec.command[0], spec.image_id, *spec.command[1:]]


HEAD_GIT = ("status", "--porcelain=v2", "--branch")


def head_argv(docker: Sequence[str], *, name: str, run_id: str, image_id: str, workdir: Path, user: str,
              git: Sequence[str] = HEAD_GIT) -> list[str]:
    """git (status by default) inside a hardened, network-less container over a read-only mount of the working copy."""
    return [*docker, "run", "--rm", "--name", name, "--network", "none", *_hardening(user, run_id, "64m"),
            "--mount", f"type=bind,source={workdir},target={WORKSPACE},readonly", "--workdir", WORKSPACE,
            "--cpus", "1", "--memory", "256m", "--memory-swap", "256m", "--pids-limit", "64",
            "--log-driver", "none", "--env", "GIT_CONFIG_NOSYSTEM=1", "--env", "GIT_TERMINAL_PROMPT=0",
            "--entrypoint", "git", image_id, "--no-optional-locks", "-c", "safe.directory=*",
            "-c", "core.fsmonitor=false", *git]


def proxy_argv(docker: Sequence[str], *, name: str, run_id: str, network: str, image: str,
               allowlist: EgressAllowlist) -> list[str]:
    allow: list[str] = []
    for host in allowlist.hosts():
        for port in sorted(allowlist.entries[host]):
            allow += ["--allow", f"{host}:{port}"]
    return [*docker, "run", "--detach", "--name", name, "--network", network, "--network-alias", PROXY_ALIAS,
            *_hardening("65534:65534", run_id, "16m"), "--cpus", "0.5", "--memory", "128m", "--memory-swap", "128m",
            "--pids-limit", "64", "--entrypoint", "python", image, "-m", "brain.execution.egress",
            "--listen", f"0.0.0.0:{PROXY_PORT}", *allow]


def docker_env() -> dict[str, str]:
    """Only what the docker CLI needs to reach the daemon; no other server variable is inherited."""
    return {name: os.environ[name] for name in _DOCKER_ENV if os.environ.get(name)}


def _safe_mount_path(path: Path) -> Path:
    if _UNSAFE_MOUNT_CHARS & set(str(path)):
        raise ExecutorError("The execution root path contains characters docker mounts cannot express")
    return path


@dataclass
class _RunState:
    image_id: str | None = None
    network: str | None = None
    proxy: str | None = None


class ContainerBackend(SubprocessBackend):
    """Clone-per-run working copy (as the subprocess backend) with every step in a hardened container."""

    backend_name = "container"

    def __init__(self, config: ContainerSettings | None = None, **kwargs: Any):
        super().__init__(**kwargs)
        self.config = config or ContainerSettings()
        self.name = self.backend_name
        self.uses_environments = True
        # Egress is enforceable only when a proxy image is configured.
        self.enforces_egress = bool(self.config.proxy_image)
        self._runs: dict[str, _RunState] = {}

    # -------------------------------------------------------------- docker helpers

    def _docker(self, args: list[str], *, timeout: float = _DOCKER_TIMEOUT) -> subprocess.CompletedProcess:
        try:
            return subprocess.run([*self.config.docker, *args], env=docker_env(), stdin=subprocess.DEVNULL,
                                  capture_output=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ExecutorError("The docker CLI is not available to the executor") from exc

    def _quiet(self, args: list[str]) -> None:
        try:
            self._docker(args, timeout=30)
        except ExecutorError:
            pass

    def resolve_environment(self, environment: Any) -> ResolvedEnvironment:
        if not isinstance(environment, Environment):
            raise ExecutorError("An execution environment is required with the container backend")
        result = self._docker(["image", "inspect", "--", environment.image])
        if result.returncode != 0:
            raise ExecutorError(f"The image of execution environment {environment.name} is not available locally")
        try:
            data = json.loads(result.stdout.decode("utf-8", "replace"))
            entry = data[0] if isinstance(data, list) else data
            image_id = str(entry["Id"])
            digests = tuple(str(item) for item in (entry.get("RepoDigests") or []))
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise ExecutorError("docker image inspect returned an unexpected result") from None
        if not _IMAGE_ID.match(image_id):
            raise ExecutorError("docker image inspect returned an unexpected image ID")
        return ResolvedEnvironment(environment=environment, image_id=image_id, repo_digests=digests)

    # -------------------------------------------------------------- lifecycle

    def prepare(self, root: Path, run_id: str, source: Path, base_sha: str, branch: str) -> WorkingCopy:
        _safe_mount_path(root.resolve() / run_id)
        copy = super().prepare(root, run_id, source, base_sha, branch)
        if sys.platform != "win32":
            os.chmod(copy.run_dir, 0o700)
        return copy

    def _state(self, copy: WorkingCopy) -> _RunState:
        return self._runs.setdefault(str(copy.run_dir), _RunState())

    def _ensure_egress(self, copy: WorkingCopy, hosts: Sequence[str]) -> str:
        state = self._state(copy)
        if state.network is not None:
            return state.network
        if not self.config.proxy_image:
            raise ExecutorError("Egress is not available: no egress proxy image is configured")
        allowlist = EgressAllowlist.parse(hosts)
        run_id = copy.run_dir.name
        network, proxy = f"brain-exec-{run_id}", f"brain-egress-{run_id}"
        state.network = network
        if self._docker(["network", "create", "--internal", "--label", f"{RUN_LABEL}={run_id}", network]).returncode:
            state.network = None
            raise ExecutorError("Could not create the execution network")
        state.proxy = proxy
        started = self._docker(proxy_argv((), name=proxy, run_id=run_id, network=network,
                                          image=self.config.proxy_image, allowlist=allowlist))
        ready = started.returncode == 0 and self._docker(
            ["network", "connect", self.config.upstream_network, proxy]).returncode == 0
        deadline = time.monotonic() + _PROXY_READY_SECONDS
        marker = json.dumps({"event": READY_EVENT})[1:-1]
        while ready:
            logs = self._docker(["logs", proxy], timeout=15)
            if marker in logs.stdout.decode("utf-8", "replace"):
                break
            if time.monotonic() >= deadline:
                ready = False
                break
            time.sleep(0.25)
        if not ready:
            self.release(copy)
            raise ExecutorError("The egress proxy did not start")
        return network

    def release(self, copy: WorkingCopy) -> None:
        state = self._runs.pop(str(copy.run_dir), None)
        if state is None:
            return
        if state.proxy:
            self._quiet(["rm", "--force", state.proxy])
        if state.network:
            self._quiet(["network", "rm", state.network])

    def reap(self, run_id: str) -> None:
        """Remove every container and network labelled with ``run_id`` (orphan recovery). Never raises."""
        label = f"label={RUN_LABEL}={run_id}"
        try:
            containers = self._docker(["ps", "--all", "--quiet", "--filter", label], timeout=30)
            ids = containers.stdout.decode("utf-8", "replace").split() if containers.returncode == 0 else []
            if ids:
                self._quiet(["rm", "--force", *ids])
            networks = self._docker(["network", "ls", "--quiet", "--filter", label], timeout=30)
            ids = networks.stdout.decode("utf-8", "replace").split() if networks.returncode == 0 else []
            if ids:
                self._quiet(["network", "rm", *ids])
        except ExecutorError:
            pass

    # -------------------------------------------------------------- steps

    def _write_secrets(self, copy: WorkingCopy, step: StepRequest) -> Path | None:
        if not step.secrets:
            return None
        directory = copy.run_dir / "secrets" / f"step-{step.index:03d}"
        directory.mkdir(parents=True, mode=0o700)
        for ref, value in step.secrets.items():
            target = directory / ref
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                handle.write(value)
        if sys.platform != "win32":
            os.chmod(directory, 0o500)
        return directory

    @staticmethod
    def _remove_secrets(directory: Path | None) -> None:
        if directory is None:
            return
        try:
            os.chmod(directory, 0o700)
            for item in directory.iterdir():
                os.chmod(item, 0o600)
                item.unlink()
            directory.rmdir()
        except OSError:
            pass

    async def run_step(self, copy: WorkingCopy, step: StepRequest, should_stop: StopProbe) -> StepOutcome:
        resolved = step.environment
        if not isinstance(resolved, ResolvedEnvironment):
            raise ExecutorError("An execution environment is required with the container backend")
        if step.tool.name not in resolved.environment.tools:
            raise ExecutorError(f"Tool not available in environment {resolved.environment.name}: {step.tool.name}")
        state = self._state(copy)
        state.image_id = resolved.image_id
        network = await asyncio.to_thread(self._ensure_egress, copy, step.egress) if step.egress else None
        run_id = copy.run_dir.name
        name = f"brain-exec-{run_id}-s{step.index:03d}"
        env = resolved.environment
        secrets_dir = await asyncio.to_thread(self._write_secrets, copy, step)
        spec = StepContainer(name=name, run_id=run_id, image_id=resolved.image_id,
                             workdir=_safe_mount_path(copy.path), user=self.config.user,
                             cpus=env.cpus or self.config.cpus, memory=env.memory or self.config.memory,
                             pids=env.pids or self.config.pids, tmpfs_size=self.config.tmpfs_size,
                             command=(*step.tool.argv, *step.args), network=network,
                             secrets_dir=_safe_mount_path(secrets_dir) if secrets_dir else None)
        argv = [*step.tool.argv, *step.args]
        try:
            result = await supervise(step_argv(self.config.docker, spec), cwd=copy.run_dir, env=docker_env(),
                                     step=step, should_stop=should_stop,
                                     on_stop=lambda: self._quiet(["kill", name]),
                                     start_error="The docker CLI could not be started")
        finally:
            await asyncio.to_thread(self._quiet, ["rm", "--force", name])
            await asyncio.to_thread(self._remove_secrets, secrets_dir)
        if result.error is None and not result.timed_out and not result.stopped and result.exit_code == 125:
            result.error = "The step container could not be started (docker exit code 125)"
        runtime = {"backend": self.name, "container": name, "environment": env.name, "image_id": resolved.image_id,
                   "network": network or "none", "secrets_mounted": sorted(step.secrets)}
        return finish_step(copy, step, argv, result, runtime=runtime)

    def head(self, copy: WorkingCopy) -> tuple[str, bool]:
        state = self._runs.get(str(copy.run_dir))
        if state is None or state.image_id is None:
            # No container has touched the working copy, so host git is still safe.
            return super().head(copy)
        run_id = copy.run_dir.name
        result = self._docker(head_argv((), name=f"brain-exec-{run_id}-head", run_id=run_id,
                                        image_id=state.image_id, workdir=_safe_mount_path(copy.path),
                                        user=self.config.user), timeout=_GIT_TIMEOUT_SECONDS)
        if result.returncode != 0:
            self._quiet(["rm", "--force", f"brain-exec-{run_id}-head"])
            raise ExecutorError("Could not read the working copy head (the environment image needs git)")
        return parse_status(result.stdout.decode("utf-8", "replace"))

    def changed_paths(self, copy: WorkingCopy) -> list[str]:
        state = self._runs.get(str(copy.run_dir))
        if state is None or state.image_id is None:
            return super().changed_paths(copy)
        if not SHA_PATTERN.match(copy.base_sha or ""):
            raise ExecutorError("The task base SHA is not a full commit SHA")
        run_id, outputs = copy.run_dir.name, []
        for index, args in enumerate(CHANGED_PATHS_GIT):
            name = f"brain-exec-{run_id}-changes{index}"
            git = [*args, copy.base_sha] if args[0] == "diff" else list(args)
            result = self._docker(head_argv((), name=name, run_id=run_id, image_id=state.image_id,
                                            workdir=_safe_mount_path(copy.path), user=self.config.user, git=git),
                                  timeout=_GIT_TIMEOUT_SECONDS)
            if result.returncode != 0:
                self._quiet(["rm", "--force", name])
                raise ExecutorError("Could not list the working copy changes (the environment image needs git)")
            outputs.append(result.stdout.decode("utf-8", "replace"))
        return parse_changed_paths(outputs)

    def patch(self, copy: WorkingCopy, limit: int) -> tuple[bytes, bool]:
        """git runs inside a container (the ``.git`` is untrusted); untracked files are read here without
        following symlinks."""
        state = self._runs.get(str(copy.run_dir))
        if state is None or state.image_id is None:
            return super().patch(copy, limit)
        if not SHA_PATTERN.match(copy.base_sha or ""):
            raise ExecutorError("The task base SHA is not a full commit SHA")
        run_id, outputs = copy.run_dir.name, []
        for index, git in enumerate(([*PATCH_GIT, copy.base_sha], list(CHANGED_PATHS_GIT[1]))):
            name = f"brain-exec-{run_id}-patch{index}"
            result = self._docker(head_argv((), name=name, run_id=run_id, image_id=state.image_id,
                                            workdir=_safe_mount_path(copy.path), user=self.config.user, git=git),
                                  timeout=_GIT_TIMEOUT_SECONDS)
            if result.returncode != 0:
                self._quiet(["rm", "--force", name])
                raise ExecutorError("Could not build the working copy patch (the environment image needs git)")
            outputs.append(result.stdout)
        tracked, untracked = outputs
        return build_patch(tracked[:limit], len(tracked), copy.path,
                           parse_changed_paths([untracked.decode("utf-8", "replace")]), limit)


def parse_status(output: str) -> tuple[str, bool]:
    """``git status --porcelain=v2 --branch`` → (HEAD SHA, dirty)."""
    sha, dirty = "", False
    for line in output.splitlines():
        if line.startswith("# branch.oid "):
            sha = line.split(" ", 2)[2].strip()
        elif line and not line.startswith("#"):
            dirty = True
    if not SHA_PATTERN.match(sha):
        raise ExecutorError("Could not read the working copy head")
    return sha, dirty


def describe(config: ContainerSettings, environments: Mapping[str, Environment]) -> dict:
    """Static description of the container configuration (no docker calls)."""
    return {"docker": list(config.docker), "user": config.user, "cpus": config.cpus, "memory": config.memory,
            "pids": config.pids, "tmpfs_size": config.tmpfs_size, "egress_proxy": bool(config.proxy_image),
            "upstream_network": config.upstream_network,
            "environments": {name: {"image": env.image, "tools": sorted(env.tools)}
                             for name, env in sorted(environments.items())}}
