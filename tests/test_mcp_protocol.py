"""MCP protocol surface beyond tools: annotations, resources, prompts,
elicitation and sampling - exercised through a real client session."""

import json
from unittest.mock import AsyncMock, patch

import pytest
from mcp.shared.memory import create_connected_server_and_client_session
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
        assert tool.annotations.readOnlyHint is not None, tool.name
        assert "ctx" not in tool.inputSchema.get("properties", {}), tool.name


@pytest.mark.asyncio
@pytest.mark.parametrize("mcp", SERVERS)
async def test_read_and_write_hints_match_behaviour(mcp):
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    assert tools["search_code"].annotations.readOnlyHint is True
    assert tools["record_decision"].annotations.readOnlyHint is False
    assert tools["record_rule"].annotations.destructiveHint is False


@pytest.mark.asyncio
@pytest.mark.parametrize("mcp", SERVERS)
async def test_resources_and_prompts_are_listed(mcp):
    uris = {str(resource.uri) for resource in await mcp.list_resources()}
    assert {"brain://rules", "brain://decisions"} <= uris
    prompts = {prompt.name for prompt in await mcp.list_prompts()}
    assert prompts == {"plan_change", "review_changes", "capture_lesson"}


@pytest.mark.asyncio
async def test_prompt_renders_with_arguments():
    async with create_connected_server_and_client_session(remote_server.mcp) as client:
        prompt = await client.get_prompt("plan_change", {"change": "rename the auth module", "repo_path": "/r"})
    text = prompt.messages[0].content.text
    assert "rename the auth module" in text
    assert "/r" in text


@pytest.mark.asyncio
async def test_rules_resource_keeps_only_active_rows():
    rows = [{"name": "keep", "status": "active"}, {"name": "drop", "status": "superseded"}, {"name": "no-status"}]
    with patch.object(remote_server, "_get", AsyncMock(return_value=rows)) as get:
        async with create_connected_server_and_client_session(remote_server.mcp) as client:
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
async def test_write_confirmation_is_opt_in(monkeypatch):
    monkeypatch.delenv(CONFIRM_WRITES_ENV, raising=False)
    callback, calls = _elicit("decline")
    add = AsyncMock(return_value=5)
    with patch("apps.mcp_server.server.DecisionStore.add_decision", add):
        async with create_connected_server_and_client_session(server.mcp, elicitation_callback=callback) as client:
            await client.call_tool("record_decision", {"title": "Use FastMCP", "repo_path": "/r"})
    assert calls == []
    add.assert_awaited_once()


@pytest.mark.asyncio
async def test_approved_write_is_stored(monkeypatch):
    monkeypatch.setenv(CONFIRM_WRITES_ENV, "true")
    callback, calls = _elicit("accept", {"approve": True, "note": ""})
    add = AsyncMock(return_value=5)
    with patch("apps.mcp_server.server.DecisionStore.add_decision", add):
        async with create_connected_server_and_client_session(server.mcp, elicitation_callback=callback) as client:
            await client.call_tool("record_decision", {"title": "Use FastMCP", "repo_path": "/r"})
    assert len(calls) == 1
    assert "Use FastMCP" in calls[0].message
    add.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,content", [("decline", None), ("cancel", None), ("accept", {"approve": False, "note": "wrong layer"})]
)
async def test_refused_write_is_not_stored(monkeypatch, action, content):
    monkeypatch.setenv(CONFIRM_WRITES_ENV, "true")
    callback, _ = _elicit(action, content)
    post = AsyncMock(return_value={"id": 1})
    with patch.object(remote_server, "_post", post):
        async with create_connected_server_and_client_session(
            remote_server.mcp, elicitation_callback=callback
        ) as client:
            result = await client.call_tool("record_rule", {"name": "No raw SQL", "repo_path": "/r"})
    post.assert_not_awaited()
    assert "cancelled" in _text(result)


@pytest.mark.asyncio
async def test_client_without_elicitation_writes_as_before(monkeypatch):
    monkeypatch.setenv(CONFIRM_WRITES_ENV, "true")
    post = AsyncMock(return_value={"id": 1})
    with patch.object(remote_server, "_post", post):
        async with create_connected_server_and_client_session(remote_server.mcp) as client:
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
async def test_ask_falls_back_to_client_sampling_when_server_model_fails():
    prompts = []

    async def sampling(_context, params):
        prompts.append(params.messages[0].content.text)
        return CreateMessageResult(
            role="assistant", content=TextContent(type="text", text="Scopes live in principals.py."), model="client"
        )

    failure = {"error": "/ask request failed: TimeoutError", "status": "failed"}
    with patch.object(remote_server, "_post", side_effect=_routes(failure)):
        async with create_connected_server_and_client_session(remote_server.mcp, sampling_callback=sampling) as client:
            result = await client.call_tool("ask_project", {"query": "where are scopes checked?", "repo_path": "/r"})
    assert _text(result) == "Scopes live in principals.py."
    assert "where are scopes checked?" in prompts[0]
    assert "brain/auth/principals.py" in prompts[0]


@pytest.mark.asyncio
async def test_ask_uses_server_answer_without_sampling():
    async def sampling(_context, _params):
        raise AssertionError("sampling must not run when /ask answered")

    with patch.object(remote_server, "_post", side_effect=_routes({"answer": "From the server."})):
        async with create_connected_server_and_client_session(remote_server.mcp, sampling_callback=sampling) as client:
            result = await client.call_tool("ask_project", {"query": "q", "repo_path": "/r"})
    assert _text(result) == "From the server."


@pytest.mark.asyncio
async def test_ask_reports_the_original_error_when_client_cannot_sample():
    failure = {"error": "/ask request failed: TimeoutError", "status": "failed"}
    with patch.object(remote_server, "_post", side_effect=_routes(failure)):
        async with create_connected_server_and_client_session(remote_server.mcp) as client:
            result = await client.call_tool("ask_project", {"query": "q", "repo_path": "/r"})
    assert "TimeoutError" in _text(result)
