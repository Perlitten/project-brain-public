"""M2 container backend, execution environments and the egress allowlist proxy.

Everything here runs without Docker: the docker CLI is replaced by a fake
(``FAKE_DOCKER``) that records its argv and emulates ``run`` by starting the
entrypoint on the host. ``test_real_docker_*`` runs only when a Docker daemon
and the image are available locally.
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import update

from brain.config.settings import settings
from brain.execution import egress
from brain.execution.bounded import StepRequest, ToolSpec
from brain.execution.container import (ContainerBackend, ContainerSettings, Environment, ResolvedEnvironment,
                                       StepContainer, head_argv, parse_status, proxy_argv, step_argv, valid_user)
from brain.execution.egress import EgressAllowlist, EgressDenied, EgressProxy, check_destination
from brain.team import execution
from brain.team.execution import ExecutionDenied, ExecutionRunner
from brain.team.models import AgentRunExecution
from tests import test_team_execution as base
from tests.test_team_execution import PY, SECRET_VALUE, TOOLS, grant, make_run, payload, request

# Shared fixtures from the subprocess-backend suite (assigned, so pytest collects them here too).
team_db = base.team_db
source_repo = base.source_repo
enabled = base.enabled

IMAGE_ID = "sha256:" + "ab" * 32
DIGEST = "brain-exec-test@sha256:" + "cd" * 32
CONTAINER_TOOLS = {
    **TOOLS,
    "readsecret": {"argv": [PY, "-c", "import os,pathlib; d=pathlib.Path(os.environ['BRAIN_SECRETS_DIR']); "
                                      "print('files=' + ','.join(sorted(p.name for p in d.iterdir())) + "
                                      "' len=' + str(len((d / 'token').read_text())))"]},
}
ENVIRONMENTS = {"py": {"image": "brain-exec-test:1", "tools": ["hello", "touch", "leak", "slow", "fail", "readsecret"]},
                "small": {"image": "brain-exec-test:1", "tools": ["hello"], "cpus": "0.5", "memory": "256m",
                          "pids": 32},
                "missing": {"image": "missing:image", "tools": ["hello"]}}

FAKE_DOCKER = r'''
import json, os, pathlib, signal, subprocess, sys
LOG = pathlib.Path(__LOG__)
args = sys.argv[1:]
with LOG.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps({"argv": args, "env": sorted(os.environ)}) + "\n")
if args[:2] == ["image", "inspect"]:
    if args[-1] == "missing:image":
        sys.exit(1)
    print(json.dumps([{"Id": __IMAGE_ID__, "RepoDigests": [__DIGEST__]}]))
    sys.exit(0)
if args[:1] == ["logs"]:
    print(json.dumps({"event": "ready", "listen": "0.0.0.0:3128"}))
    sys.exit(0)
if args[:1] == ["kill"]:
    pid_file = LOG.parent / (args[1] + ".pid")
    if pid_file.exists():
        os.kill(int(pid_file.read_text()), signal.SIGTERM)
    sys.exit(0)
if args[:1] == ["run"] and "--detach" not in args:
    env, mounts, name, command, i = {}, {}, None, None, 1
    while i < len(args):
        flag = args[i]
        if flag == "--entrypoint":
            command = [args[i + 1], *args[i + 3:]]
            break
        if flag in ("--rm", "--init", "--read-only"):
            i += 1
            continue
        value = args[i + 1]
        if flag == "--env":
            key, _, val = value.partition("=")
            env[key] = val
        elif flag == "--mount":
            parts = dict(part.split("=", 1) for part in value.split(",") if "=" in part)
            mounts[parts["target"]] = parts["source"]
        elif flag == "--name":
            name = value
        i += 2
    child_env = {key: os.environ[key] for key in ("SYSTEMROOT", "PATH") if key in os.environ}
    child_env.update({key: mounts.get(val, val) for key, val in env.items()})
    child = subprocess.Popen(command, cwd=mounts["/workspace"], env=child_env)
    (LOG.parent / (name + ".pid")).write_text(str(child.pid))
    sys.exit(child.wait())
sys.exit(0)
'''


def _fake_docker(tmp_path: Path) -> tuple[tuple[str, ...], Path]:
    state = tmp_path / "fake-docker"
    state.mkdir(exist_ok=True)
    log = state / "calls.jsonl"
    script = state / "docker.py"
    script.write_text(FAKE_DOCKER.replace("__LOG__", repr(str(log))).replace("__IMAGE_ID__", repr(IMAGE_ID))
                      .replace("__DIGEST__", repr(DIGEST)), encoding="utf-8")
    return (PY, str(script)), log


def _calls(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]


@pytest.fixture
def container(enabled, monkeypatch, tmp_path):
    """Container backend settings with the fake docker CLI; yields (backend factory, call log)."""
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_BACKEND", "container")
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_TOOLS", json.dumps(CONTAINER_TOOLS))
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_ENVIRONMENTS", json.dumps(ENVIRONMENTS))
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_CONTAINER_USER", "1000:1000")
    docker, log = _fake_docker(tmp_path)

    def backend(**overrides) -> ContainerBackend:
        return ContainerBackend(ContainerSettings(docker=docker, user="1000:1000", **overrides))

    return backend, log


# --------------------------------------------------------------------------- exact docker argv


def _spec(**overrides) -> StepContainer:
    values = dict(name="brain-exec-r1-s000", run_id="r1", image_id=IMAGE_ID, workdir=Path("/srv/exec/r1/repo"),
                  user="1000:1000", cpus="1", memory="1g", pids=256, tmpfs_size="256m",
                  command=("python", "-m", "pytest", "-q"))
    values.update(overrides)
    return StepContainer(**values)


HARDENING = ["--pull", "never", "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m", "--user", "1000:1000",
             "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--label", "brain.execution.run=r1",
             "--env", "HOME=/tmp", "--env", "TMPDIR=/tmp", "--env", "LANG=C.UTF-8",
             "--env", "PYTHONDONTWRITEBYTECODE=1"]


def test_step_argv_is_exact_and_denies_the_network_by_default():
    assert step_argv(("docker",), _spec()) == [
        "docker", "run", "--rm", "--init", "--name", "brain-exec-r1-s000", "--network", "none", *HARDENING,
        "--mount", f"type=bind,source={Path('/srv/exec/r1/repo')},target=/workspace", "--workdir", "/workspace",
        "--cpus", "1", "--memory", "1g", "--memory-swap", "1g", "--pids-limit", "256", "--log-driver", "none",
        "--env", "GIT_CONFIG_NOSYSTEM=1", "--env", "GIT_TERMINAL_PROMPT=0",
        "--entrypoint", "python", IMAGE_ID, "-m", "pytest", "-q"]


def test_step_argv_mounts_secrets_read_only_and_routes_egress_through_the_proxy():
    secrets = Path("/srv/exec/r1/secrets/step-000")
    argv = step_argv(("docker",), _spec(network="brain-exec-r1", secrets_dir=secrets))
    assert argv[:9] == ["docker", "run", "--rm", "--init", "--name", "brain-exec-r1-s000", "--network",
                        "brain-exec-r1", "--pull"]
    tail = argv[argv.index("GIT_TERMINAL_PROMPT=0") + 1:]
    proxy = "http://brain-egress:3128"
    assert tail == [
        "--mount", f"type=bind,source={secrets},target=/run/brain-secrets,readonly",
        "--env", "BRAIN_SECRETS_DIR=/run/brain-secrets",
        "--env", f"HTTP_PROXY={proxy}", "--env", f"HTTPS_PROXY={proxy}", "--env", f"http_proxy={proxy}",
        "--env", f"https_proxy={proxy}", "--env", "NO_PROXY=", "--env", "no_proxy=",
        "--entrypoint", "python", IMAGE_ID, "-m", "pytest", "-q"]


def test_head_and_proxy_argv_are_exact():
    assert head_argv(("docker",), name="brain-exec-r1-head", run_id="r1", image_id=IMAGE_ID,
                     workdir=Path("/srv/exec/r1/repo"), user="1000:1000") == [
        "docker", "run", "--rm", "--name", "brain-exec-r1-head", "--network", "none",
        *[item.replace("size=256m", "size=64m") for item in HARDENING],
        "--mount", f"type=bind,source={Path('/srv/exec/r1/repo')},target=/workspace,readonly",
        "--workdir", "/workspace", "--cpus", "1", "--memory", "256m", "--memory-swap", "256m", "--pids-limit", "64",
        "--log-driver", "none", "--env", "GIT_CONFIG_NOSYSTEM=1", "--env", "GIT_TERMINAL_PROMPT=0",
        "--entrypoint", "git", IMAGE_ID, "--no-optional-locks", "-c", "safe.directory=*", "-c",
        "core.fsmonitor=false", "status", "--porcelain=v2", "--branch"]
    allowlist = EgressAllowlist.parse(["pypi.org", "files.example.com:8443"])
    assert proxy_argv(("docker",), name="brain-egress-r1", run_id="r1", network="brain-exec-r1",
                      image="brain:latest", allowlist=allowlist) == [
        "docker", "run", "--detach", "--name", "brain-egress-r1", "--network", "brain-exec-r1",
        "--network-alias", "brain-egress",
        *[item.replace("size=256m", "size=16m").replace("1000:1000", "65534:65534") for item in HARDENING],
        "--cpus", "0.5", "--memory", "128m", "--memory-swap", "128m", "--pids-limit", "64",
        "--entrypoint", "python", "brain:latest", "-m", "brain.execution.egress", "--listen", "0.0.0.0:3128",
        "--allow", "files.example.com:8443", "--allow", "pypi.org:80", "--allow", "pypi.org:443"]


def test_parse_status_and_user_validation():
    sha = "a" * 40
    assert parse_status(f"# branch.oid {sha}\n# branch.head main\n") == (sha, False)
    assert parse_status(f"# branch.oid {sha}\n? new.txt\n") == (sha, True)
    with pytest.raises(Exception, match="head"):
        parse_status("# branch.oid (initial)\n")
    assert valid_user("1000:1000") and valid_user("65534")
    assert not valid_user("0") and not valid_user("1000:0") and not valid_user("root") and not valid_user("1000:abc")


# --------------------------------------------------------------------------- egress allowlist


def test_egress_allowlist_is_exact_hosts_and_ports():
    allowlist = EgressAllowlist.parse(["PyPI.org., files.example.com:8443", "10.0.0.5:5432", ""])
    assert allowlist.hosts() == ["10.0.0.5", "files.example.com", "pypi.org"]
    assert allowlist.allows("pypi.org", 443) and allowlist.allows("pypi.org", 80)
    assert not allowlist.allows("pypi.org", 22) and not allowlist.allows("files.example.com", 443)
    assert allowlist.allows("files.example.com", 8443) and allowlist.allows("10.0.0.5", 5432)
    assert not allowlist.allows("sub.pypi.org", 443) and not allowlist.allows("pypi.org.evil.test", 443)
    for bad in ("*.example.com", "exa mple.com", "example.com:0", "example.com:99999", "example.com:x", "-a.com"):
        with pytest.raises(ValueError):
            EgressAllowlist.parse([bad])


@pytest.mark.asyncio
async def test_check_destination_refuses_unlisted_and_non_public_destinations():
    allowlist = EgressAllowlist.parse(["pypi.org", "internal.example", "10.0.0.5:5432", "nowhere.example"])

    def resolver(answers):
        async def resolve(host, port):
            if answers is None:
                raise OSError("no such host")
            return answers
        return resolve

    assert await check_destination(allowlist, "PYPI.org", 443, resolver=resolver(["151.101.0.223", "2a04:4e42::223"])) == [
        "151.101.0.223", "2a04:4e42::223"]
    with pytest.raises(EgressDenied, match="not in the egress allowlist"):
        await check_destination(allowlist, "example.com", 443, resolver=resolver(["93.184.216.34"]))
    with pytest.raises(EgressDenied, match="not in the egress allowlist"):
        await check_destination(allowlist, "pypi.org", 22, resolver=resolver(["151.101.0.223"]))
    for private in (["127.0.0.1"], ["10.1.2.3"], ["169.254.169.254"], ["151.101.0.223", "192.168.1.1"], ["::1"]):
        with pytest.raises(EgressDenied, match="non-public"):
            await check_destination(allowlist, "internal.example", 443, resolver=resolver(private))
    assert await check_destination(allowlist, "internal.example", 443, resolver=resolver(["10.1.2.3"]),
                                   allow_private=True) == ["10.1.2.3"]
    # An IP literal the operator listed is used as is.
    assert await check_destination(allowlist, "10.0.0.5", 5432, resolver=resolver(None)) == ["10.0.0.5"]
    with pytest.raises(EgressDenied) as unresolved:
        await check_destination(allowlist, "nowhere.example", 443, resolver=resolver(None))
    assert unresolved.value.status == 502


async def _exchange(port: int, data: bytes, *, then: bytes = b"") -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(data)
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
    if then:
        writer.write(then)
        await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
    body = await asyncio.wait_for(reader.read(), 10)
    writer.close()
    return head + body


@pytest.mark.asyncio
async def test_egress_proxy_tunnels_allowlisted_destinations_and_refuses_the_rest(capsys):
    received: list[bytes] = []

    async def upstream(reader, writer):
        data = await reader.read(65536)
        received.append(data)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok" if data.startswith(b"GET ") else b"echo:" + data)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(upstream, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    async def resolve(host, _port):
        return ["127.0.0.2" if sys.platform != "win32" else "0.0.0.1", "127.0.0.1"]  # the first one refuses

    proxy = EgressProxy(EgressAllowlist.parse([f"allowed.test:{port}"]), resolver=resolve, allow_private=True)
    proxy_server = await proxy.start("127.0.0.1", 0)
    proxy_port = proxy_server.sockets[0].getsockname()[1]
    try:
        tunnel = await _exchange(proxy_port, f"CONNECT allowed.test:{port} HTTP/1.1\r\nHost: x\r\n\r\n".encode(),
                                 then=b"ping")
        assert tunnel.startswith(b"HTTP/1.1 200 Connection established\r\n\r\n") and tunnel.endswith(b"echo:ping")
        plain = await _exchange(proxy_port, (f"GET http://allowed.test:{port}/simple?x=1 HTTP/1.1\r\n"
                                             f"Host: allowed.test\r\nProxy-Authorization: Basic c2VjcmV0\r\n"
                                             f"Proxy-Connection: keep-alive\r\nAccept: */*\r\n\r\n").encode())
        assert plain.startswith(b"HTTP/1.1 200 OK") and plain.endswith(b"ok")
        assert received[-1] == (b"GET /simple?x=1 HTTP/1.1\r\nHost: allowed.test\r\nAccept: */*\r\n"
                                b"Connection: close\r\n\r\n")
        denied = await _exchange(proxy_port, b"CONNECT other.test:443 HTTP/1.1\r\n\r\n")
        assert denied.startswith(b"HTTP/1.1 403 Forbidden") and b"not in the egress allowlist" in denied
        wrong_port = await _exchange(proxy_port, b"CONNECT allowed.test:443 HTTP/1.1\r\n\r\n")
        assert wrong_port.startswith(b"HTTP/1.1 403")
        relative = await _exchange(proxy_port, b"GET /local HTTP/1.1\r\nHost: allowed.test\r\n\r\n")
        assert relative.startswith(b"HTTP/1.1 400")
        assert len(received) == 2  # denied requests never reached the upstream
    finally:
        proxy_server.close()
        server.close()
    decisions = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    assert [d["decision"] for d in decisions] == ["allow", "allow", "deny", "deny", "deny"]


@pytest.mark.asyncio
async def test_egress_proxy_refuses_hosts_resolving_to_private_addresses():
    async def resolve(host, _port):
        return ["169.254.169.254"]

    proxy = EgressProxy(EgressAllowlist.parse(["metadata.test"]), resolver=resolve)
    server = await proxy.start("127.0.0.1", 0)
    try:
        reply = await _exchange(server.sockets[0].getsockname()[1], b"CONNECT metadata.test:443 HTTP/1.1\r\n\r\n")
    finally:
        server.close()
    assert reply.startswith(b"HTTP/1.1 403") and b"non-public" in reply


def test_egress_proxy_cli_requires_an_allowlist():
    with pytest.raises(SystemExit):
        egress.main([])
    with pytest.raises(SystemExit):
        egress.main(["--allow", "*.example.com"])


# --------------------------------------------------------------------------- operator policy and health


@pytest.mark.parametrize("name, value, message", [
    ("BRAIN_EXECUTION_BACKEND", "vm", "BRAIN_EXECUTION_BACKEND must be one of"),
    ("BRAIN_EXECUTION_ENVIRONMENTS", "[]", "must be a JSON object"),
    ("BRAIN_EXECUTION_ENVIRONMENTS", json.dumps({"py": {"image": "x:1", "tools": ["nope"]}}),
     "missing from BRAIN_EXECUTION_TOOLS: nope"),
    ("BRAIN_EXECUTION_ENVIRONMENTS", json.dumps({"py": {"image": "x:1", "tools": [], "privileged": True}}),
     "invalid execution environment: py"),
    ("BRAIN_EXECUTION_ENVIRONMENTS", json.dumps({"py": {"image": "x y", "tools": []}}), "needs a valid image"),
    ("BRAIN_EXECUTION_ENVIRONMENTS", json.dumps({"py": {"image": "x:1", "tools": [], "memory": "lots"}}),
     "invalid cpus, memory or pids"),
    ("BRAIN_EXECUTION_CONTAINER_USER", "0:0", "non-root"),
    ("BRAIN_EXECUTION_CONTAINER_MEMORY", "0", "invalid BRAIN_EXECUTION_CONTAINER"),
    ("BRAIN_EXECUTION_EGRESS_UPSTREAM_NETWORK", "bad network", "UPSTREAM_NETWORK"),
])
def test_misconfigured_container_settings_disable_execution(container, monkeypatch, name, value, message):
    monkeypatch.setattr(settings, name, value)
    policy = execution.operator_policy()
    assert policy.error and policy.error.startswith("Execution is misconfigured") and message in policy.error
    assert policy.disabled_reason() == policy.error and policy.environments == {}


def test_container_policy_and_default_backend(container, monkeypatch):
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_EGRESS_PROXY_IMAGE", "brain:latest")
    policy = execution.operator_policy()
    assert policy.error is None and policy.backend == "container"
    assert policy.environments["small"] == Environment(name="small", image="brain-exec-test:1",
                                                       tools=frozenset({"hello"}), cpus="0.5", memory="256m", pids=32)
    backend = execution.default_backend(policy)
    assert isinstance(backend, ContainerBackend) and backend.name == "container" and backend.uses_environments
    assert backend.enforces_egress and backend.config.user == "1000:1000"
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_EGRESS_PROXY_IMAGE", "")
    assert execution.default_backend().enforces_egress is False
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_BACKEND", "subprocess")
    assert execution.default_backend().name == "subprocess"


def test_execution_config_health(container, monkeypatch):
    environ = {"FIXTURE_EXEC_TOKEN": "set"}
    health = execution.execution_config_health(environ=environ)
    assert health["status"] == "ok" and health["backend"] == "container" and health["egress_enforced"] is False
    assert health["environments"] == ["missing", "py", "small"]
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_EGRESS", "pypi.org")
    health = execution.execution_config_health(environ={})
    checks = {check["name"]: check for check in health["checks"]}
    assert health["status"] == "warn" and checks["egress"]["status"] == "warn"
    assert checks["secrets"]["detail"] == "Secret references without a server value: token"
    assert SECRET_VALUE not in json.dumps(health)
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_EGRESS_PROXY_IMAGE", "brain:latest")
    assert execution.execution_config_health(environ=environ)["egress_enforced"] is True
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_ENVIRONMENTS", "{}")
    assert execution.execution_config_health(environ=environ)["status"] == "error"
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_BACKEND", "subprocess")
    health = execution.execution_config_health(environ=environ)
    assert health["status"] == "warn" and {c["name"]: c["status"] for c in health["checks"]}["backend"] == "warn"
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_ENABLED", False)
    assert execution.execution_config_health(environ=environ) == {"status": "disabled", "enabled": False,
                                                                 "backend": "subprocess", "checks": []}
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_BACKEND", "vm")
    assert execution.execution_config_health(environ=environ)["status"] == "error"


# --------------------------------------------------------------------------- environment gates


@pytest.mark.asyncio
@pytest.mark.parametrize("body, message, proxy", [
    (payload("hello"), "An execution environment is required with the container backend", ""),
    (payload("hello", environment="gpu"), "Unknown execution environment: gpu", ""),
    (payload("leak", environment="small"), "Tool not available in environment small: leak", ""),
    (payload("hello", environment="bad name"), "The execution environment is a lowercase name", ""),
    (payload("hello", environment="py", egress=["pypi.org"]), "Egress is not available", ""),
    (payload("hello", environment="py", egress=["example.com"]), "Egress destination not allowed", "brain:latest"),
])
async def test_container_requests_need_a_known_environment(team_db, source_repo, container, monkeypatch, body,
                                                            message, proxy):
    factory, a, _ = team_db
    backend, _log = container
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_EGRESS", "pypi.org")
    task_id, run_id, _ = await make_run(factory, a, source_repo[1])
    await grant(factory, a)
    with pytest.raises(ExecutionDenied, match=message):
        await request(factory, a, task_id, run_id, body, backend=backend(proxy_image=proxy))


@pytest.mark.asyncio
async def test_subprocess_backend_refuses_environments(team_db, source_repo, container, monkeypatch):
    factory, a, _ = team_db
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_BACKEND", "subprocess")
    task_id, run_id, _ = await make_run(factory, a, source_repo[1])
    await grant(factory, a)
    with pytest.raises(ExecutionDenied, match="Execution environments need the container backend"):
        await request(factory, a, task_id, run_id, payload("hello", environment="py"))
    # Without an environment the subprocess backend behaves as before (and records none).
    state = await request(factory, a, task_id, run_id, payload("hello"))
    assert state["state"] == "requested" and state["environment"] is None and state["backend"] == "subprocess"
    async with factory() as session:
        assert (await session.get(AgentRunExecution, run_id)).environment is None


@pytest.mark.asyncio
async def test_child_runs_stay_in_the_parent_environment(team_db, source_repo, container):
    from brain.auth.principals import ResolvedPrincipal
    from brain.team.models import AgentRun
    from brain.team.store import request_run

    factory, a, _ = team_db
    backend, _log = container
    task_id, parent_id, _ = await make_run(factory, a, source_repo[1])
    await grant(factory, a)
    async with factory() as session:
        row = await session.get(AgentRun, parent_id)
    parent_caller = ResolvedPrincipal(row.principal_id, "run", "run", scopes=frozenset({"team:read", "team:run.request"}),
                                      credential_id=row.credential_id)
    async with factory() as session, session.begin():
        child = await request_run(session, parent_caller, a, task_id, dict(
            branch="brain/child", permissions=["read"], idempotency_key="child-run-env"))
    child_id = uuid.UUID(child["id"])

    async def child_request(body):
        async with factory() as session, session.begin():
            return await execution.request_execution(session, parent_caller, a, task_id, child_id, body,
                                                     backend=backend())

    await request(factory, a, task_id, parent_id, payload("hello", environment="small"), backend=backend())
    with pytest.raises(ExecutionDenied, match="Execution environment not allowed for this run"):
        await child_request(payload("hello", environment="py"))
    assert (await child_request(payload("hello", environment="small")))["state"] == "requested"


# --------------------------------------------------------------------------- execution through the fake docker CLI


def _kind(event: dict) -> str:
    return event["type"].removeprefix("run.execution.")


def _payloads(state: dict, kind: str) -> list[dict]:
    return [event["payload"] for event in state["events"] if _kind(event) == kind]


def _runs(calls: list[dict]) -> list[list[str]]:
    return [call["argv"] for call in calls if call["argv"][:1] == ["run"]]


@pytest.mark.asyncio
async def test_container_run_records_environment_egress_and_runtime_evidence(team_db, source_repo, container):
    factory, a, _ = team_db
    backend, log = container
    task_id, run_id, _ = await make_run(factory, a, source_repo[1])
    await grant(factory, a)
    await request(factory, a, task_id, run_id, payload(
        "hello", {"tool": "leak", "secrets": ["token"]}, {"tool": "readsecret", "secrets": ["token"]},
        {"tool": "touch", "args": ["made-in-container.txt"]}, environment="py"), backend=backend())
    async with factory() as session:
        assert (await session.get(AgentRunExecution, run_id)).environment == "py"
    state = await execution.execute_run(run_id, factory=factory, backend=backend())
    assert state["state"] == "completed", state["events"]
    events = {_kind(event): event["payload"] for event in state["events"]}
    assert events["requested"]["environment"] == "py"
    started = events["started"]
    assert started["backend"] == "container"
    assert started["environment"] == {"name": "py", "image": "brain-exec-test:1", "image_id": IMAGE_ID,
                                      "repo_digests": [DIGEST]}
    assert started["egress"] == {"profile": "none", "hosts": []}
    steps = _payloads(state, "step")
    assert [step["exit_code"] for step in steps] == [0, 0, 0, 0]
    assert steps[0]["runtime"] == {"backend": "container", "container": f"brain-exec-{run_id}-s000",
                                   "environment": "py", "image_id": IMAGE_ID, "network": "none",
                                   "secrets_mounted": []}
    assert steps[1]["runtime"]["secrets_mounted"] == ["token"]
    assert "token=missing" in steps[1]["excerpt"]  # secrets are never environment variables
    assert f"files=token len={len(SECRET_VALUE)}" in steps[2]["excerpt"]  # but readable as mounted files
    assert steps[3]["argv"] == [*CONTAINER_TOOLS["touch"]["argv"], "made-in-container.txt"]  # the tool argv, not docker's

    calls = _calls(log)
    assert calls[0]["argv"] == ["image", "inspect", "--", "brain-exec-test:1"]
    runs = _runs(calls)
    # After the four steps: head, the changed paths (diff against base, untracked files), then the final patch
    # (tracked diff, untracked files), all read inside hardened, network-less containers over a read-only mount;
    # .git is untrusted once a step ran.
    assert len(runs) == 9 and all(argv[argv.index("--entrypoint") + 1] == "git" for argv in runs[4:])
    assert [argv[argv.index("--name") + 1] for argv in runs[5:]] == [
        f"brain-exec-{run_id}-changes0", f"brain-exec-{run_id}-changes1", f"brain-exec-{run_id}-patch0",
        f"brain-exec-{run_id}-patch1"]
    assert runs[7][-7:] == ["diff", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames", "--full-index",
                            source_repo[1]]
    assert all(argv[argv.index("--network") + 1] == "none" and argv[argv.index("--mount") + 1].endswith(",readonly")
               for argv in runs[4:])
    assert runs[5][-5:] == ["diff", "--name-only", "-z", "--no-renames", source_repo[1]]
    for index, argv in enumerate(runs[:4]):
        name = f"brain-exec-{run_id}-s{index:03d}"
        assert argv[argv.index("--name") + 1] == name and argv[argv.index("--network") + 1] == "none"
        assert argv[argv.index("--user") + 1] == "1000:1000" and IMAGE_ID in argv
        assert ["rm", "--force", name] in [call["argv"] for call in calls]
    assert any(item.endswith("target=/run/brain-secrets,readonly") for item in runs[1])
    assert not any("brain-secrets" in item for item in runs[0])
    text = log.read_text(encoding="utf-8")
    assert SECRET_VALUE not in text and "FIXTURE_EXEC_TOKEN" not in text  # neither argv nor the docker CLI's env
    completed = events["completed"]
    assert (Path(completed["worktree_path"]) / "made-in-container.txt").exists() and completed["dirty"] is True
    assert not list((Path(completed["worktree_path"]).parent / "secrets").glob("*/*"))  # removed after each step
    assert completed["head_sha"] == source_repo[1]
    assert completed["changed_paths"] == ["made-in-container.txt"] and completed["changed_paths_total"] == 1
    assert completed["overlaps"] == 0


@pytest.mark.asyncio
async def test_container_egress_runs_behind_a_per_run_proxy(team_db, source_repo, container, monkeypatch):
    factory, a, _ = team_db
    backend, log = container
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_EGRESS", "pypi.org,files.pythonhosted.org")
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_EGRESS_PROXY_IMAGE", "brain:latest")
    task_id, run_id, _ = await make_run(factory, a, source_repo[1])
    await grant(factory, a)
    configured = backend(proxy_image="brain:latest")
    await request(factory, a, task_id, run_id, payload("hello", "hello", environment="py", egress=["pypi.org"]),
                  backend=configured)
    state = await execution.execute_run(run_id, factory=factory, backend=configured)
    assert state["state"] == "completed", state["events"]
    started = _payloads(state, "started")[0]
    assert started["egress"] == {"profile": "allowlist", "hosts": ["pypi.org"]}
    network, proxy = f"brain-exec-{run_id}", f"brain-egress-{run_id}"
    argvs = [call["argv"] for call in _calls(log)]
    assert ["network", "create", "--internal", "--label", f"brain.execution.run={run_id}", network] in argvs
    proxy_run = next(argv for argv in argvs if "--detach" in argv)
    assert proxy_run[proxy_run.index("--network") + 1] == network and "brain:latest" in proxy_run
    assert proxy_run[-4:] == ["--allow", "pypi.org:80", "--allow", "pypi.org:443"]  # only the run's hosts
    assert ["network", "connect", "bridge", proxy] in argvs
    steps = [argv for argv in _runs(_calls(log)) if "--detach" not in argv and "git" not in argv]
    assert len(steps) == 2 and argvs.count(["network", "create", "--internal", "--label",
                                            f"brain.execution.run={run_id}", network]) == 1  # one proxy per run
    for argv in steps:
        assert argv[argv.index("--network") + 1] == network
        assert "HTTPS_PROXY=http://brain-egress:3128" in argv
    # Torn down at the end of the run: proxy first, then the network.
    assert argvs.index(["rm", "--force", proxy]) < argvs.index(["network", "rm", network])
    assert _payloads(state, "step")[0]["runtime"]["network"] == network


@pytest.mark.asyncio
async def test_missing_image_fails_before_any_container_runs(team_db, source_repo, container):
    factory, a, _ = team_db
    backend, log = container
    task_id, run_id, _ = await make_run(factory, a, source_repo[1])
    await grant(factory, a)
    await request(factory, a, task_id, run_id, payload("hello", environment="missing"), backend=backend())
    state = await execution.execute_run(run_id, factory=factory, backend=backend())
    assert state["state"] == "failed"
    final = state["events"][-1]["payload"]
    assert final["phase"] == "environment" and "missing is not available locally" in final["reason"]
    assert not _runs(_calls(log)) and not _payloads(state, "started")


@pytest.mark.asyncio
async def test_docker_unavailable_fails_the_run(team_db, source_repo, container):
    factory, a, _ = team_db
    _backend, _log = container
    task_id, run_id, _ = await make_run(factory, a, source_repo[1])
    await grant(factory, a)
    broken = ContainerBackend(ContainerSettings(docker=(str(Path(source_repo[0]) / "no-such-docker"),),
                                                user="1000:1000"))
    await request(factory, a, task_id, run_id, payload("hello", environment="py"), backend=broken)
    state = await execution.execute_run(run_id, factory=factory, backend=broken)
    assert state["state"] == "failed"
    assert state["events"][-1]["payload"]["reason"] == "The docker CLI is not available to the executor"


@pytest.mark.asyncio
async def test_timeout_kills_and_removes_the_step_container(team_db, source_repo, container):
    factory, a, _ = team_db
    backend, log = container
    task_id, run_id, _ = await make_run(factory, a, source_repo[1])
    await grant(factory, a)
    await request(factory, a, task_id, run_id, payload("slow", environment="py", budget={"step_timeout_seconds": 2}),
                  backend=backend())
    started = asyncio.get_running_loop().time()
    state = await execution.execute_run(run_id, factory=factory, backend=backend())
    assert asyncio.get_running_loop().time() - started < 20
    assert state["state"] == "failed" and state["events"][-1]["payload"]["reason"] == "The step exceeded its timeout"
    name = f"brain-exec-{run_id}-s000"
    argvs = [call["argv"] for call in _calls(log)]
    assert argvs.index(["kill", name]) < argvs.index(["rm", "--force", name])
    assert not list(Path(settings.BRAIN_EXECUTION_ROOT).rglob("late.txt"))


@pytest.mark.asyncio
async def test_revocation_kills_the_step_container(team_db, source_repo, container, monkeypatch):
    factory, a, _ = team_db
    backend, log = container
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_LEASE_SECONDS", 1)
    task_id, run_id, _ = await make_run(factory, a, source_repo[1])
    await grant(factory, a)
    await request(factory, a, task_id, run_id, payload("slow", "hello", environment="py"), backend=backend())

    async def revoke_soon():
        await asyncio.sleep(1.5)
        async with factory() as session, session.begin():
            await execution.revoke_grant(session, a, 2, "operator:test")

    revoker = asyncio.create_task(revoke_soon())
    state = await execution.execute_run(run_id, factory=factory, backend=backend())
    await revoker
    assert state["state"] == "revoked", state["events"]
    assert state["events"][-1]["payload"]["in_flight"]["evidence"]["stopped"] == "revoked"
    argvs = [call["argv"] for call in _calls(log)]
    assert ["kill", f"brain-exec-{run_id}-s000"] in argvs
    assert not any(f"brain-exec-{run_id}-s001" in argv for argv in argvs)


@pytest.mark.asyncio
async def test_backend_change_after_the_request_stops_the_run(team_db, source_repo, container, monkeypatch):
    factory, a, _ = team_db
    backend, log = container
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_BACKEND", "subprocess")
    task_id, run_id, _ = await make_run(factory, a, source_repo[1])
    await grant(factory, a)
    await request(factory, a, task_id, run_id, payload("hello"))
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_BACKEND", "container")
    state = await execution.execute_run(run_id, factory=factory, backend=backend())
    assert state["state"] == "revoked"
    assert state["events"][-1]["payload"]["reason"] == "The executor backend changed since the request"
    assert _calls(log) == []


@pytest.mark.asyncio
async def test_orphaned_container_runs_are_reaped_by_label(team_db, source_repo, container):
    factory, a, _ = team_db
    backend, log = container
    task_id, run_id, _ = await make_run(factory, a, source_repo[1])
    await grant(factory, a)
    await request(factory, a, task_id, run_id, payload("hello", environment="py"), backend=backend())
    policy = execution.operator_policy()
    lease = await execution.acquire_lease(factory, run_id, "crashed", lease_seconds=30)
    runner = ExecutionRunner(factory, lease, a, task_id, policy, backend())

    async def started(session, row, run):
        execution._event(session, run, "started", f"{lease.token}:started", runner.actor, {})

    assert (await runner.checkpoint(started))[0] == "ok"
    async with factory() as session, session.begin():
        await session.execute(update(AgentRunExecution).where(AgentRunExecution.run_id == run_id)
                              .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=5)))
    reaped: list[str] = []
    assert await execution.recover_orphaned_runs(factory, reaper=reaped.append) == [str(run_id)]
    assert reaped == [str(run_id)]
    backend().reap(str(run_id))
    label = f"label=brain.execution.run={run_id}"
    assert [call["argv"] for call in _calls(log)] == [["ps", "--all", "--quiet", "--filter", label],
                                                      ["network", "ls", "--quiet", "--filter", label]]


@pytest.mark.asyncio
async def test_run_step_refuses_tools_outside_the_environment(tmp_path, container):
    backend, log = container
    resolved = ResolvedEnvironment(Environment(name="small", image="x:1", tools=frozenset({"hello"})), IMAGE_ID)
    step = StepRequest(index=0, tool=ToolSpec(name="leak", argv=("true",)), args=(), secrets={}, timeout_seconds=5,
                       output_cap=1024, environment=resolved)
    with pytest.raises(Exception, match="Tool not available in environment small: leak"):
        await backend().run_step(None, step, lambda: None)
    with pytest.raises(Exception, match="environment is required"):
        await backend().run_step(None, StepRequest(index=0, tool=step.tool, args=(), secrets={}, timeout_seconds=5,
                                                   output_cap=1024), lambda: None)
    assert _calls(log) == []


# --------------------------------------------------------------------------- real docker (optional)


REAL_IMAGE = os.getenv("BRAIN_TEST_EXECUTION_IMAGE", "python:3.12-slim")


def _docker_ready() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        info = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True, timeout=20)
        image = subprocess.run(["docker", "image", "inspect", REAL_IMAGE], capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return info.returncode == 0 and image.returncode == 0


@pytest.mark.asyncio
@pytest.mark.skipif(not _docker_ready(), reason=f"needs a reachable Docker daemon and the local image {REAL_IMAGE}")
async def test_real_docker_step_is_isolated(tmp_path, source_repo):
    repo, sha = source_repo
    backend = ContainerBackend(ContainerSettings(), source_roots=lambda: [tmp_path])
    resolved = await asyncio.to_thread(backend.resolve_environment,
                                       Environment(name="py", image=REAL_IMAGE, tools=frozenset({"probe", "sleep"})))
    copy = await asyncio.to_thread(backend.prepare, tmp_path / "executions", uuid.uuid4().hex, repo, sha, "brain/real")
    visible = subprocess.run(["docker", "run", "--rm", "--network", "none", "--mount",
                              f"type=bind,source={copy.path},target=/probe,readonly", "--entrypoint", "ls",
                              REAL_IMAGE, "/probe"], capture_output=True, text=True, timeout=60)
    if "README.md" not in visible.stdout:
        pytest.skip("the Docker daemon cannot bind-mount this host's paths (remote or WSL daemon)")
    probe = ToolSpec(name="probe", argv=("python", "-c", (
        "import os, pathlib, socket\n"
        "print('uid', os.getuid())\n"
        "try:\n    pathlib.Path('/etc/brain-probe').write_text('x'); print('root-writable')\n"
        "except OSError: print('root-read-only')\n"
        "try:\n    socket.create_connection(('1.1.1.1', 53), 3); print('network-open')\n"
        "except OSError: print('network-closed')\n"
        "print('secret-env', 'BRAIN_SECRET_TOKEN' in os.environ)\n"
        "print('secret-file', len((pathlib.Path(os.environ['BRAIN_SECRETS_DIR']) / 'token').read_text()))\n"
        "pathlib.Path('written.txt').write_text('x')\n")))

    async def never():
        return None

    try:
        outcome = await backend.run_step(copy, StepRequest(index=0, tool=probe, args=(), secrets={"token": SECRET_VALUE},
                                                           timeout_seconds=60, output_cap=65536,
                                                           environment=resolved), never)
        assert outcome.exit_code == 0, outcome.excerpt
        assert "root-read-only" in outcome.excerpt and "network-closed" in outcome.excerpt
        assert "uid 0" not in outcome.excerpt and "secret-env False" in outcome.excerpt
        assert f"secret-file {len(SECRET_VALUE)}" in outcome.excerpt and SECRET_VALUE not in outcome.excerpt
        assert (copy.path / "written.txt").exists()
        sleep = ToolSpec(name="sleep", argv=("python", "-c", "import time; time.sleep(120)"))
        timed = await backend.run_step(copy, StepRequest(index=1, tool=sleep, args=(), secrets={}, timeout_seconds=3,
                                                         output_cap=1024, environment=resolved), never)
        assert timed.timed_out
        name = f"brain-exec-{copy.run_dir.name}-s001"
        left = subprocess.run(["docker", "ps", "--all", "--quiet", "--filter", f"name={name}"], capture_output=True,
                              text=True, timeout=30)
        assert left.stdout.strip() == ""
    finally:
        backend.release(copy)
        backend.reap(copy.run_dir.name)
    assert not list((copy.run_dir / "secrets").glob("*/*"))
