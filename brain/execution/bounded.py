"""Bounded executor backends for team runs (M2, docs/operations/TEAM_EXECUTION.md).

A backend knows how to give a run its own working copy and how to run one
allowlisted tool invocation in it. It knows nothing about authority: the
caller (``brain.team.execution``) re-authorizes before every call and decides
whether a result may be committed.

Two backends implement the interface: ``SubprocessBackend`` (below) and
``ContainerBackend`` (``brain.execution.container``), selected by
``BRAIN_EXECUTION_BACKEND``. ``SubprocessBackend``:

- the working copy is a fresh ``git clone`` of the source repository into
  ``<root>/<run id>/repo``, checked out at the run's base SHA on the run's own
  branch, with the ``origin`` remote removed so nothing can be pushed back;
- each step is ``argv`` from the operator's tool definition plus validated
  arguments, started without a shell in its own process group, with the
  working copy as ``cwd`` (confined by ``PathSandbox``), a minimal environment
  that inherits no server secrets, a timeout and an output cap;
- output is kept up to the cap (the tail), secret values are redacted, and the
  kept output is written to ``<root>/<run id>/logs`` with its SHA-256.

It does **not** contain the process: a tool can read what the server user can
read and open network connections. Worktree isolation is not security
containment (ADR). ``enforces_egress`` is therefore ``False`` and the caller
refuses any run that asks for egress. ``ContainerBackend`` runs each step in a
hardened container (no network by default, read-only root, secrets as mounted
files) and enforces an egress allowlist through ``brain.execution.egress``.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence

from brain.config.paths import allowed_repo_roots, resolve_within
from brain.execution.cancellation import ExecutionCancellationManager
from brain.workspace.path_sandbox import PathSandbox, PathValidationError

SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
EXCERPT_CHARS = 4000
REDACTED = "[REDACTED]"
_DEAD_PROXY = "http://127.0.0.1:9"
_GIT_TIMEOUT_SECONDS = 120
# Changed paths listed after a run (for overlap detection); a larger change set is truncated.
MAX_CHANGED_PATHS = 5000
# What the run changed against its base: tracked changes (committed or not) and untracked, unignored files.
CHANGED_PATHS_GIT = (("diff", "--name-only", "-z", "--no-renames"), ("ls-files", "--others", "--exclude-standard", "-z"))


class ExecutorError(RuntimeError):
    """A backend could not do what was asked; the message is safe to record."""


@dataclass(frozen=True)
class ToolSpec:
    """One operator-allowlisted tool: a fixed argv prefix and whether callers may append arguments."""
    name: str
    argv: tuple[str, ...]
    allow_args: bool = False


@dataclass(frozen=True)
class WorkingCopy:
    run_dir: Path
    path: Path
    branch: str
    base_sha: str


@dataclass(frozen=True)
class StepRequest:
    index: int
    tool: ToolSpec
    args: tuple[str, ...]
    secrets: Mapping[str, str]
    timeout_seconds: float
    output_cap: int
    check_interval: float = 1.0
    # Container backend only: the resolved environment (``container.ResolvedEnvironment``) and the egress hosts.
    environment: Any = None
    egress: tuple[str, ...] = ()


@dataclass
class StepOutcome:
    index: int
    tool: str
    argv: list[str]
    exit_code: int | None
    duration_ms: int
    output_bytes: int
    truncated: bool
    output_sha256: str
    excerpt: str
    timed_out: bool = False
    stopped: str | None = None
    error: str | None = None
    log_path: str | None = None
    runtime: dict | None = None

    def evidence(self) -> dict:
        evidence = {"index": self.index, "tool": self.tool, "argv": self.argv, "exit_code": self.exit_code,
                    "duration_ms": self.duration_ms, "output_bytes": self.output_bytes, "truncated": self.truncated,
                    "output_sha256": self.output_sha256, "excerpt": self.excerpt, "timed_out": self.timed_out,
                    "stopped": self.stopped, "error": self.error}
        if self.runtime is not None:
            evidence["runtime"] = self.runtime
        return evidence


StopProbe = Callable[[], Awaitable[str | None]]


class ExecutorBackend(Protocol):
    name: str
    enforces_egress: bool
    # True when every execution must name an operator-defined environment (image + tools).
    uses_environments: bool

    def resolve_environment(self, environment: Any) -> Any:
        """Pin an environment to what will actually run (an image ID); ``None`` for backends without one."""
        ...

    def prepare(self, root: Path, run_id: str, source: Path, base_sha: str, branch: str) -> WorkingCopy:
        ...

    async def run_step(self, copy: WorkingCopy, step: StepRequest, should_stop: StopProbe) -> StepOutcome:
        ...

    def head(self, copy: WorkingCopy) -> tuple[str, bool]:
        ...

    def changed_paths(self, copy: WorkingCopy) -> list[str]:
        """Repository-relative paths the run changed against ``copy.base_sha`` (raises ``ExecutorError``)."""
        ...

    def release(self, copy: WorkingCopy) -> None:
        """Tear down per-run resources (networks, sidecars). Idempotent; never raises."""
        ...


def validate_args(args: Sequence[str]) -> tuple[str, ...]:
    """Lexical confinement of caller arguments: no paths outside the working copy, no control bytes."""
    checked = []
    for arg in args:
        if not isinstance(arg, str) or len(arg) > 1024:
            raise ExecutorError("Tool arguments must be strings of at most 1024 characters")
        if any(ch in arg for ch in ("\x00", "\n", "\r")):
            raise ExecutorError("Tool arguments must not contain control characters")
        for token in arg.split("="):
            normalized = token.replace("\\", "/")
            if (normalized.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", normalized)
                    or ".." in normalized.split("/")):
                raise ExecutorError("Tool arguments must stay inside the working copy")
        checked.append(arg)
    return tuple(checked)


def redact(text: str, secrets: Sequence[str], *, truncated_head: bool = False) -> str:
    for value in sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True):
        text = text.replace(value, REDACTED)
        if truncated_head:
            # A cut at the start of the kept tail can leave the end of a secret behind.
            for size in range(len(value) - 1, 3, -1):
                if text.startswith(value[-size:]):
                    text = REDACTED + text[size:]
                    break
    return text


def minimal_env(run_dir: Path, secrets: Mapping[str, str]) -> dict[str, str]:
    """Only what a tool needs to start; no server variable is inherited except PATH (and Windows basics)."""
    home = run_dir / "home"
    tmp = run_dir / "tmp"
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(home), "USERPROFILE": str(home),
        "TMPDIR": str(tmp), "TMP": str(tmp), "TEMP": str(tmp),
        "LANG": "C.UTF-8", "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_ALLOW_PROTOCOL": "file",
        # Not a network boundary: tools that honour proxy variables fail fast instead of reaching out.
        "HTTP_PROXY": _DEAD_PROXY, "HTTPS_PROXY": _DEAD_PROXY, "ALL_PROXY": _DEAD_PROXY,
        "http_proxy": _DEAD_PROXY, "https_proxy": _DEAD_PROXY, "all_proxy": _DEAD_PROXY, "NO_PROXY": "",
    }
    if sys.platform == "win32":
        for name in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"):
            if os.environ.get(name):
                env[name] = os.environ[name]
    for ref, value in secrets.items():
        env[f"BRAIN_SECRET_{ref.upper()}"] = value
    return env


class _TailBuffer:
    def __init__(self, cap: int):
        self.cap = max(1, cap)
        self.total = 0
        self._chunks = bytearray()
        self._lock = threading.Lock()

    def feed(self, chunk: bytes) -> None:
        with self._lock:
            self.total += len(chunk)
            self._chunks.extend(chunk)
            if len(self._chunks) > self.cap:
                del self._chunks[: len(self._chunks) - self.cap]

    def value(self) -> bytes:
        with self._lock:
            return bytes(self._chunks)


def _drain(stream, buffer: _TailBuffer) -> None:
    try:
        for chunk in iter(lambda: stream.read(8192), b""):
            buffer.feed(chunk)
    except (OSError, ValueError):
        pass


@dataclass
class ProcessResult:
    exit_code: int | None
    timed_out: bool
    stopped: str | None
    error: str | None
    buffer: _TailBuffer
    started: float


async def supervise(argv: list[str], *, cwd: Path, env: Mapping[str, str], step: StepRequest, should_stop: StopProbe,
                    on_stop: Callable[[], None] | None = None,
                    start_error: str = "The tool executable could not be started") -> ProcessResult:
    """Run ``argv`` without a shell in its own process group under the step's timeout and stop probe.

    ``on_stop`` runs (in a thread) before the process group is terminated, for
    work the local process cannot do itself, such as killing a container."""
    buffer = _TailBuffer(step.output_cap)
    started = time.monotonic()
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    exit_code: int | None = None
    timed_out = False
    stopped: str | None = None
    error: str | None = None
    try:
        process = subprocess.Popen(argv, cwd=str(cwd), env=dict(env), stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=False, close_fds=True,
                                   **kwargs)
    except OSError:
        process = None
        error = start_error
    if process is not None:
        reader = threading.Thread(target=_drain, args=(process.stdout, buffer), daemon=True)
        reader.start()
        next_check = started + step.check_interval
        while process.poll() is None:
            await asyncio.sleep(0.05)
            now = time.monotonic()
            if now - started >= step.timeout_seconds:
                timed_out = True
                break
            if now >= next_check:
                next_check = now + step.check_interval
                stopped = await should_stop()
                if stopped:
                    break
        if process.poll() is None:
            if on_stop is not None:
                await asyncio.to_thread(on_stop)
            if process.poll() is None:
                await asyncio.to_thread(ExecutionCancellationManager.terminate_process_group, process, 5.0)
        exit_code = process.wait()
        await asyncio.to_thread(reader.join, 5.0)
    return ProcessResult(exit_code=exit_code, timed_out=timed_out, stopped=stopped, error=error, buffer=buffer,
                         started=started)


def finish_step(copy: WorkingCopy, step: StepRequest, argv: list[str], result: ProcessResult,
                runtime: dict | None = None) -> StepOutcome:
    """Redact the kept output tail, store it under ``logs`` with its SHA-256 and build the step evidence."""
    kept = result.buffer.value()
    truncated = result.buffer.total > len(kept)
    text = redact(kept.decode("utf-8", "replace"), list(step.secrets.values()), truncated_head=truncated)
    stored = text.encode("utf-8")
    checksum = hashlib.sha256(stored).hexdigest()
    log_path = copy.run_dir / "logs" / f"step-{step.index:03d}.log"
    log_path.write_bytes(stored)
    return StepOutcome(index=step.index, tool=step.tool.name, argv=argv, exit_code=result.exit_code,
                       duration_ms=int((time.monotonic() - result.started) * 1000), output_bytes=result.buffer.total,
                       truncated=truncated, output_sha256=checksum, excerpt=text[-EXCERPT_CHARS:],
                       timed_out=result.timed_out, stopped=result.stopped, error=result.error,
                       log_path=str(log_path), runtime=runtime)


def parse_changed_paths(outputs: Sequence[str]) -> list[str]:
    """NUL-separated ``git diff --name-only -z`` / ``git ls-files -z`` outputs → sorted unique paths (capped)."""
    paths = {item for output in outputs for item in output.split("\0") if item}
    return sorted(paths)[:MAX_CHANGED_PATHS]


@dataclass
class SubprocessBackend:
    """Clone-per-run working copy and allowlisted subprocesses; no container, no network boundary."""
    name: str = "subprocess"
    enforces_egress: bool = False
    uses_environments: bool = False
    git: str = "git"
    source_roots: Callable[[], list[Path]] = field(default=allowed_repo_roots)

    def _git(self, args: list[str], cwd: Path | None, run_dir: Path, *, strip: bool = True) -> str:
        try:
            result = subprocess.run([self.git, *args], cwd=str(cwd) if cwd else None, env=minimal_env(run_dir, {}),
                                    stdin=subprocess.DEVNULL, capture_output=True, timeout=_GIT_TIMEOUT_SECONDS,
                                    check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ExecutorError(f"git {args[0]} could not be run") from exc
        if result.returncode != 0:
            raise ExecutorError(f"git {args[0]} failed with exit code {result.returncode}")
        output = result.stdout.decode("utf-8", "replace")
        return output.strip() if strip else output

    def prepare(self, root: Path, run_id: str, source: Path, base_sha: str, branch: str) -> WorkingCopy:
        if not SHA_PATTERN.match(base_sha or ""):
            raise ExecutorError("The task base SHA is not a full commit SHA")
        try:
            source_path = PathSandbox(self.source_roots()).validate(source, require_git=True)
        except PathValidationError as exc:
            raise ExecutorError("The source repository is outside the allowed repository roots") from exc
        root.mkdir(parents=True, exist_ok=True)
        try:
            run_dir = resolve_within(root, run_id)
        except ValueError as exc:
            raise ExecutorError("Invalid run directory") from exc
        if run_dir.exists():
            raise ExecutorError("The run directory already exists; a run executes once")
        for sub in ("home", "tmp", "logs"):
            (run_dir / sub).mkdir(parents=True)
        self._git(["check-ref-format", "--branch", branch], None, run_dir)
        target = run_dir / "repo"
        self._git(["clone", "--no-checkout", "--no-hardlinks", "--quiet", "--", str(source_path), str(target)],
                  None, run_dir)
        try:
            confined = PathSandbox([root]).validate(target, require_git=True)
        except PathValidationError as exc:
            raise ExecutorError("The working copy escaped the execution root") from exc
        self._git(["remote", "remove", "origin"], confined, run_dir)
        self._git(["checkout", "--quiet", "-b", branch, base_sha], confined, run_dir)
        if self._git(["rev-parse", "HEAD"], confined, run_dir) != base_sha:
            raise ExecutorError("The working copy is not at the task base SHA")
        return WorkingCopy(run_dir=run_dir, path=confined, branch=branch, base_sha=base_sha)

    def head(self, copy: WorkingCopy) -> tuple[str, bool]:
        sha = self._git(["rev-parse", "HEAD"], copy.path, copy.run_dir)
        dirty = bool(self._git(["status", "--porcelain"], copy.path, copy.run_dir))
        return sha, dirty

    def changed_paths(self, copy: WorkingCopy) -> list[str]:
        if not SHA_PATTERN.match(copy.base_sha or ""):
            raise ExecutorError("The task base SHA is not a full commit SHA")
        outputs = [self._git([*args, copy.base_sha] if args[0] == "diff" else list(args), copy.path, copy.run_dir,
                             strip=False) for args in CHANGED_PATHS_GIT]
        return parse_changed_paths(outputs)

    def resolve_environment(self, environment: Any) -> Any:
        if environment is not None:
            raise ExecutorError("Execution environments need the container backend")
        return None

    def release(self, copy: WorkingCopy) -> None:
        return None

    async def run_step(self, copy: WorkingCopy, step: StepRequest, should_stop: StopProbe) -> StepOutcome:
        cwd = PathSandbox([copy.run_dir]).validate(copy.path, require_git=True)
        argv = [*step.tool.argv, *step.args]
        result = await supervise(argv, cwd=cwd, env=minimal_env(copy.run_dir, step.secrets), step=step,
                                 should_stop=should_stop)
        return finish_step(copy, step, argv, result)
