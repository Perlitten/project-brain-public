"""The tracked project MCP config must never carry a literal deployment secret.

`.mcp.json` used to be gitignored precisely because it held the live API key.
It is tracked now so that a fresh clone - including a cloud Claude Code session,
which has no local config to fall back on - gets the Brain MCP server. That is
only safe while the sensitive values stay `${VAR}` references that Claude Code
expands from the environment, so this test is the thing standing between a
careless `git add -A` and a committed API key.
"""

import json
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parents[1] / ".mcp.json"
MUST_BE_REFERENCES = ("BRAIN_API_URL", "BRAIN_API_KEY")


def _server_entry() -> dict:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return config["mcpServers"]["project-brain"]


def test_project_mcp_config_is_present_and_parses():
    assert CONFIG_PATH.is_file(), ".mcp.json must stay in the repository root"
    assert _server_entry()["args"] == ["-m", "apps.mcp_server.remote_server"]


def test_project_mcp_config_holds_no_literal_secret():
    env = _server_entry()["env"]
    for name in MUST_BE_REFERENCES:
        value = env[name]
        assert value.startswith("${") and value.endswith("}"), (
            f"{name} in .mcp.json must stay a ${{VAR}} reference resolved from the "
            f"environment, never a literal value, but it is {value!r}"
        )
