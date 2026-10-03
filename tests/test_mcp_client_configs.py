"""Generated MCP client configs must work outside the dev checkout:
proper JSON, each client's real contract, cwd-independent launch, and the
packaged ``brain-mcp`` entry point."""

import asyncio
import json
import os
import shutil
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from apps.api.routers.setup import _client_configs, _mcp_server_launch

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _configs():
    return {
        name: {"meta": entry, "server": json.loads(entry["config"])["mcpServers"]["brain"]}
        for name, entry in _client_configs().items()
    }


def test_client_configs_are_valid_json_on_the_real_contract():
    for name, cfg in _configs().items():
        server = cfg["server"]
        assert isinstance(server["command"], str) and server["command"], name
        assert isinstance(server["args"], list), name
        assert isinstance(server["env"], dict), name
        # cwd is not part of the documented client contract — the launch must
        # be self-contained instead.
        assert "cwd" not in server, name


def test_launch_contract_points_inside_this_install():
    server = _mcp_server_launch()
    assert server["command"] == sys.executable
    assert server["args"] == ["-m", "apps.mcp_server.server"]
    assert Path(server["env"]["PYTHONPATH"]).resolve() == PROJECT_ROOT.resolve()
    assert Path(server["env"]["BRAIN_ENV_FILE"]).is_absolute()
    assert Path(server["env"]["BRAIN_SETUP_STATE_DIR"]).is_absolute()


def test_brain_mcp_console_script_is_declared_and_callable():
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    assert pyproject["project"]["scripts"]["brain-mcp"] == "apps.mcp_server.server:main"
    from apps.mcp_server import server as mcp_server

    assert callable(mcp_server.main)


async def _handshake(pythonpath: str, cwd: str) -> int:
    """Spawn the server exactly as a client would and complete initialize."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "apps.mcp_server.server"],
        env={"PYTHONPATH": pythonpath},
        cwd=cwd,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            return len(tools.tools)


def test_server_launches_from_an_unrelated_working_directory(tmp_path):
    unrelated = tmp_path / "elsewhere"
    unrelated.mkdir()
    tools = asyncio.new_event_loop().run_until_complete(
        _handshake(str(PROJECT_ROOT), str(unrelated))
    )
    assert tools >= 10


def test_server_launch_survives_spaces_and_quotes_in_the_path(tmp_path):
    awkward = tmp_path / "brain space'dir"
    awkward.mkdir()
    link = awkward / "Brain"
    link.symlink_to(PROJECT_ROOT)
    tools = asyncio.new_event_loop().run_until_complete(
        _handshake(str(link), str(tmp_path))
    )
    assert tools >= 10


def test_settings_find_the_install_env_from_any_cwd(tmp_path):
    """Use a disposable install with a non-default port, not an operator's .env."""
    install = tmp_path / "install"
    shutil.copytree(
        PROJECT_ROOT / "brain",
        install / "brain",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (install / ".env").write_text("POSTGRES_PORT=5544\n")
    unrelated = tmp_path / "elsewhere"
    unrelated.mkdir()
    env = dict(os.environ)
    env.pop("POSTGRES_PORT", None)
    env.pop("BRAIN_ENV_FILE", None)
    env["PYTHONPATH"] = str(install)
    command = [
        sys.executable,
        "-c",
        "from brain.config.settings import settings; print(settings.POSTGRES_PORT)",
    ]
    result = subprocess.run(command, cwd=unrelated, env=env, capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "5544"
    (unrelated / ".env").write_text("POSTGRES_PORT=5666\n")
    result = subprocess.run(command, cwd=unrelated, env=env, capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "5666"


def test_brain_env_file_override_wins(tmp_path):
    env_file = tmp_path / "custom.env"
    env_file.write_text("POSTGRES_PORT=5999\n")
    # A process-level POSTGRES_PORT (as CI sets) outranks every dotenv file.
    env = {k: v for k, v in os.environ.items() if k != "POSTGRES_PORT"}
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from brain.config.settings import settings; print(settings.POSTGRES_PORT)",
        ],
        cwd=str(tmp_path),
        env={**env, "BRAIN_ENV_FILE": str(env_file), "PYTHONPATH": str(PROJECT_ROOT)},
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "5999"


@pytest.mark.skipif(
    shutil.which("claude") is None,
    reason="claude CLI not installed — Claude Code config validated against its documented contract only",
)
def test_claude_code_accepts_generated_config():
    pytest.skip("Live Claude Code launch verification is not implemented; JSON contract and stdio handshake are tested separately")


@pytest.mark.skipif(
    shutil.which("cursor") is None,
    reason="cursor CLI not installed — Cursor config validated against its documented contract only",
)
def test_cursor_accepts_generated_config():
    pytest.skip("Live Cursor launch verification is not implemented; JSON contract and stdio handshake are tested separately")


def test_copyable_cli_preserves_literal_paths(monkeypatch):
    from apps.api.routers import setup

    root = "/tmp/brain space'dir/$literal;still-path"
    executable = "/tmp/python space'dir/$literal"
    env_file = "/tmp/config space'dir/$literal.env"
    monkeypatch.setattr(setup, "_mcp_server_launch", lambda: {
        "command": executable, "args": ["-m", "apps.mcp_server.server"],
        "env": {"PYTHONPATH": root, "BRAIN_ENV_FILE": env_file},
    })
    configs = _client_configs()
    claude = shlex.split(configs["Claude Code"]["cli"])
    assert claude[5:10] == [f"PYTHONPATH={root}", "--env", f"BRAIN_ENV_FILE={env_file}", "--", executable]
    generic = shlex.split(configs["Any MCP client (stdio)"]["cli"], comments=True)
    assert generic == [f"PYTHONPATH={root}", f"BRAIN_ENV_FILE={env_file}", executable, "-m", "apps.mcp_server.server"]


def test_launch_preserves_explicit_instance_configuration(tmp_path, monkeypatch):
    config = tmp_path / "instance.env"
    state = tmp_path / "evidence"
    monkeypatch.setenv("BRAIN_ENV_FILE", str(config))
    monkeypatch.setenv("BRAIN_SETUP_STATE_DIR", str(state))
    launch = _mcp_server_launch()
    assert launch["env"]["BRAIN_ENV_FILE"] == str(config.resolve())
    assert launch["env"]["BRAIN_SETUP_STATE_DIR"] == str(state.resolve())
