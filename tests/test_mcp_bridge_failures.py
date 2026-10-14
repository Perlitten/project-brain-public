"""Operational errors are MCP errors, not successful empty data or rule rows."""
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from mcp import Client, StdioServerParameters
from mcp.server.mcpserver.exceptions import ResourceError

from apps.mcp_server import remote_server
from apps.mcp_server.protocol import only_active


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_missing_bridge_configuration_signals_tool_and_resource_errors(monkeypatch, mode):
    monkeypatch.setattr(remote_server, "_asgi_app", None)
    monkeypatch.setattr(remote_server, "BASE_URL", "")
    monkeypatch.setattr(remote_server, "API_KEY", "")
    async with Client(remote_server.mcp, mode=mode) as client:
        result = await client.call_tool("search_code", {"query": "payments"})
        assert result.is_error and "BRAIN_API_URL" in str(result)
        with pytest.raises(Exception, match="BRAIN_API_URL"):
            await client.read_resource("brain://rules")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "auto"])
@pytest.mark.parametrize("status", [401, 500, "timeout"])
@pytest.mark.parametrize("tool,arguments", [("search_code", {"query": "payments"}),
                                         ("get_background_job", {"job_id": "fixture"}),
                                         ("get_diff_review", {"job_id": "fixture"})])
async def test_http_failure_is_reported_through_real_mcp_contract(monkeypatch, mode, status, tool, arguments):
    monkeypatch.setattr(remote_server, "_asgi_app", None)
    monkeypatch.setattr(remote_server, "BASE_URL", "http://127.0.0.1:9999")
    monkeypatch.setattr(remote_server, "API_KEY", "synthetic-fixture")
    original = httpx.AsyncClient

    def respond(request):
        if status == "timeout":
            raise httpx.ReadTimeout("synthetic")
        return httpx.Response(status, json={"detail": "synthetic-secret-not-for-output"})

    monkeypatch.setattr(remote_server.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(respond), **kw))
    async with Client(remote_server.mcp, mode=mode) as client:
        result = await client.call_tool(tool, arguments)
    assert result.is_error
    assert str(status) in str(result) or "ReadTimeout" in str(result)
    assert "synthetic-secret-not-for-output" not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "auto"])
@pytest.mark.parametrize("tool", ["get_background_job", "get_diff_review"])
async def test_failed_domain_job_is_successful_polling_data(monkeypatch, mode, tool):
    monkeypatch.setattr(remote_server, "_asgi_app", None)
    monkeypatch.setattr(remote_server, "BASE_URL", "http://127.0.0.1:9999")
    monkeypatch.setattr(remote_server, "API_KEY", "synthetic-fixture")
    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"status": "failed", "error": "synthetic-job-failure"}))
    monkeypatch.setattr(remote_server.httpx, "AsyncClient", lambda **kw: original(transport=transport, **kw))
    async with Client(remote_server.mcp, mode=mode) as client:
        result = await client.call_tool(tool, {"job_id": "fixture"})
    assert not result.is_error
    assert "synthetic-job-failure" in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_successful_empty_retrieval_and_resources_remain_success(monkeypatch, mode):
    monkeypatch.setattr(remote_server, "_post", AsyncMock(return_value={"results": []}))
    monkeypatch.setattr(remote_server, "_get", AsyncMock(return_value=[]))
    async with Client(remote_server.mcp, mode=mode) as client:
        result = await client.call_tool("search_code", {"query": "unsupported"})
        resource = await client.read_resource("brain://rules")
    assert not result.is_error and "results" in str(result)
    assert json.loads(resource.contents[0].text) == []


def test_failure_envelope_never_becomes_a_rule():
    with pytest.raises(ResourceError):
        only_active({"status": "failed", "error": "unreachable"})


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_real_stdio_missing_configuration_has_error_semantics(tmp_path, mode):
    root = Path(__file__).resolve().parents[1]
    params = StdioServerParameters(command=sys.executable, args=["-m", "apps.mcp_server.remote_server"],
        cwd=str(tmp_path), env={"PYTHONPATH": str(root), "BRAIN_API_URL": "", "BRAIN_API_KEY": "",
            "BRAIN_SETUP_STATE_DIR": str(tmp_path / "setup"), "DEFAULT_LLM_PROVIDER": "disabled",
            "DEFAULT_EMBEDDING_PROVIDER": "local", "REPO_ROOT": str(tmp_path), "TARGET_REPO_PATH": str(tmp_path)})
    async with Client(params, mode=mode) as client:
        result = await client.call_tool("search_code", {"query": "payments"})
        assert result.is_error and "BRAIN_API_URL" in str(result)
        with pytest.raises(Exception, match="BRAIN_API_URL"):
            await client.read_resource("brain://rules")
