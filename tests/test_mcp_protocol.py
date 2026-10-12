"""MCP protocol surface beyond tools: annotations, resources, prompts,
elicitation and sampling - exercised through a real client session."""

import json
from unittest.mock import AsyncMock, patch

import pytest
from mcp import Client
from mcp.types import CreateMessageResult, ElicitResult, TextContent

from apps.mcp_server import remote_server, server
from apps.mcp_server.protocol import CONFIRM_WRITES_ENV, TOOL_ANNOTATIONS

SERVERS = [pytest.param(server.mcp, id="local"), pytest.param(remote_server.mcp, id="remote")]


def _text(result) -> str:
    return "".join(block.text for block in result.content if isinstance(block, TextContent))


@pytest.mark.asyncio
@pytest.mark.parametrize("mcp", SERVERS)
async def test_every_tool_is_annotated_and_hides_context(mcp):
    tools = await mcp.list_tools()
    assert tools
    for tool in tools:
        assert tool.name in TOOL_ANNOTATIONS, tool.name
        assert tool.title, tool.name
        assert tool.annotations is not None, tool.name
        assert tool.annotations.read_only_hint is not None, tool.name
        assert "ctx" not in tool.input_schema.get("properties", {}), tool.name


@pytest.mark.asyncio
@pytest.mark.parametrize("mcp", SERVERS)
async def test_read_and_write_hints_match_behaviour(mcp):
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    assert tools["search_code"].annotations.read_only_hint is True
    assert tools["record_decision"].annotations.read_only_hint is False
    assert tools["record_rule"].annotations.destructive_hint is False


@pytest.mark.asyncio
@pytest.mark.parametrize("mcp", SERVERS)
async def test_resources_and_prompts_are_listed(mcp):
    uris = {str(resource.uri) for resource in await mcp.list_resources()}
    assert {"brain://rules", "brain://decisions"} <= uris
    prompts = {prompt.name for prompt in await mcp.list_prompts()}
    assert prompts == {"plan_change", "review_changes", "capture_lesson"}


@pytest.mark.asyncio
async def test_prompt_renders_with_arguments():
    async with Client(remote_server.mcp, mode="legacy") as client:
        prompt = await client.get_prompt("plan_change", {"change": "rename the auth module", "repo_path": "/r"})
    text = prompt.messages[0].content.text
    assert "rename the auth module" in text
    assert "/r" in text


@pytest.mark.asyncio
async def test_rules_resource_keeps_only_active_rows():
    rows = [{"name": "keep", "status": "active"}, {"name": "drop", "status": "superseded"}, {"name": "no-status"}]
    with patch.object(remote_server, "_get", AsyncMock(return_value=rows)) as get:
        async with Client(remote_server.mcp, mode="legacy") as client:
            result = await client.read_resource("brain://rules")
    get.assert_awaited_once_with("/rules")
    names = [row["name"] for row in json.loads(result.contents[0].text)]
    assert names == ["keep", "no-status"]


# --- elicitation ---------------------------------------------------------------


def _elicit(action: str, content: dict | None = None):
    calls = []

    async def callback(_context, params):
        calls.append(params)
        return ElicitResult(action=action, content=content)

    return callback, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_write_confirmation_is_opt_in(mode, monkeypatch):
    monkeypatch.delenv(CONFIRM_WRITES_ENV, raising=False)
    callback, calls = _elicit("decline")
    add = AsyncMock(return_value=5)
    with patch("apps.mcp_server.server.DecisionStore.add_decision", add):
        async with Client(server.mcp, mode=mode, elicitation_callback=callback) as client:
            await client.call_tool("record_decision", {"title": "Use FastMCP", "repo_path": "/r"})
    assert calls == []
    add.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_approved_write_is_stored(mode, monkeypatch):
    monkeypatch.setenv(CONFIRM_WRITES_ENV, "true")
    callback, calls = _elicit("accept", {"approve": True, "note": ""})
    add = AsyncMock(return_value=5)
    with patch("apps.mcp_server.server.DecisionStore.add_decision", add):
        async with Client(server.mcp, mode=mode, elicitation_callback=callback) as client:
            await client.call_tool("record_decision", {"title": "Use FastMCP", "repo_path": "/r"})
    assert len(calls) == 1
    assert "Use FastMCP" in calls[0].message
    add.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,content", [("decline", None), ("cancel", None), ("accept", {"approve": False, "note": "wrong layer"})]
)
@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_refused_write_is_not_stored(mode, monkeypatch, action, content):
    monkeypatch.setenv(CONFIRM_WRITES_ENV, "true")
    callback, _ = _elicit(action, content)
    post = AsyncMock(return_value={"id": 1})
    with patch.object(remote_server, "_post", post):
        async with Client(
            remote_server.mcp, mode=mode, elicitation_callback=callback
        ) as client:
            result = await client.call_tool("record_rule", {"name": "No raw SQL", "repo_path": "/r"})
    post.assert_not_awaited()
    assert "cancelled" in _text(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_client_without_elicitation_writes_as_before(mode, monkeypatch):
    monkeypatch.setenv(CONFIRM_WRITES_ENV, "true")
    post = AsyncMock(return_value={"id": 1})
    with patch.object(remote_server, "_post", post):
        async with Client(remote_server.mcp, mode=mode) as client:
            await client.call_tool("record_rule", {"name": "No raw SQL", "repo_path": "/r"})
    post.assert_awaited_once()


# --- sampling ------------------------------------------------------------------


def _routes(answer_result):
    async def post(path, payload, timeout=120.0):
        if path == "/ask":
            return answer_result
        if path == "/context":
            return {"files": [{"path": "brain/auth/principals.py"}], "query": payload["task_description"]}
        raise AssertionError(path)

    return post


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_ask_falls_back_to_client_sampling_when_server_model_fails(mode):
    prompts = []

    async def sampling(_context, params):
        prompts.append(params.messages[0].content.text)
        return CreateMessageResult(
            role="assistant", content=TextContent(type="text", text="Scopes live in principals.py."), model="client"
        )

    failure = {"error": "/ask request failed: TimeoutError", "status": "failed"}
    with patch.object(remote_server, "_post", side_effect=_routes(failure)):
        async with Client(remote_server.mcp, mode=mode, sampling_callback=sampling) as client:
            result = await client.call_tool("ask_project", {"query": "where are scopes checked?", "repo_path": "/r"})
    assert _text(result) == "Scopes live in principals.py."
    assert "where are scopes checked?" in prompts[0]
    assert "brain/auth/principals.py" in prompts[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_ask_uses_server_answer_without_sampling(mode):
    async def sampling(_context, _params):
        raise AssertionError("sampling must not run when /ask answered")

    with patch.object(remote_server, "_post", side_effect=_routes({"answer": "From the server."})):
        async with Client(remote_server.mcp, mode=mode, sampling_callback=sampling) as client:
            result = await client.call_tool("ask_project", {"query": "q", "repo_path": "/r"})
    assert _text(result) == "From the server."


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_ask_reports_the_original_error_when_client_cannot_sample(mode):
    failure = {"error": "/ask request failed: TimeoutError", "status": "failed"}
    with patch.object(remote_server, "_post", side_effect=_routes(failure)):
        async with Client(remote_server.mcp, mode=mode) as client:
            result = await client.call_tool("ask_project", {"query": "q", "repo_path": "/r"})
    assert "TimeoutError" in _text(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "auto"])
@pytest.mark.parametrize("mcp", SERVERS)
async def test_wire_tool_contract_keeps_camel_case_fields(mcp, mode):
    async with Client(mcp, mode=mode) as client:
        listed = await client.list_tools()
        tools = {tool.name: tool.model_dump(by_alias=True, exclude_none=True) for tool in listed.tools}
    search = tools["search_code"]
    assert "inputSchema" in search
    assert "ctx" not in search["inputSchema"].get("properties", {})
    assert search["annotations"]["readOnlyHint"] is True
    assert tools["record_rule"]["annotations"]["destructiveHint"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_client_identity_survives_sdk_handshake(tmp_path, monkeypatch, mode):
    from mcp.types import Implementation
    from brain.onboarding import setup_state

    monkeypatch.setenv("BRAIN_SETUP_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(server, "_CONNECTED_CLIENT", None)
    monkeypatch.setattr(server.settings, "BRAIN_AGENT_CONTEXT_V2_MODE", "off")
    records = []

    async def record(**payload):
        records.append(payload)

    with (
        patch.object(server, "hybrid_search_code", AsyncMock(return_value={"chunks": []})),
        patch("apps.api.telemetry.repository_id_for_path", AsyncMock(return_value=None)),
        patch("apps.api.telemetry.record_request", record),
    ):
        async with Client(server.mcp, mode=mode, client_info=Implementation(name="Claude Code", version="1.0")) as client:
            result = await client.call_tool("search_code", {"query": "config"})
    assert not result.is_error
    assert records[0]["principal_name"] == "mcp:claude-code"
    activity = setup_state.last_client_activity()
    assert activity["client"] == "Claude Code"
    assert activity["version"] == "1.0"


@pytest.mark.asyncio
async def test_stdio_subprocess_records_real_initialize(tmp_path, monkeypatch):
    import os
    import sys
    from pathlib import Path
    from mcp import StdioServerParameters
    from mcp.types import Implementation
    from brain.onboarding import setup_state

    monkeypatch.setenv("BRAIN_SETUP_STATE_DIR", str(tmp_path))
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "apps.mcp_server.server"],
        cwd=str(Path(__file__).resolve().parents[1]), env=dict(os.environ),
    )
    async with Client(params, mode="legacy", client_info=Implementation(name="stdio-test", version="2.3")) as client:
        tools = await client.list_tools()
        assert any(tool.name == "search_code" for tool in tools.tools)
        prompt = await client.get_prompt("plan_change", {"change": "test the SDK migration"})
        assert "test the SDK migration" in prompt.messages[0].content.text
    activity = setup_state.last_client_activity()
    assert activity["client"] == "stdio-test"
    assert activity["version"] == "2.3"
