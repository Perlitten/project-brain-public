import pytest
from unittest.mock import AsyncMock, patch
from apps.mcp_server.server import mcp


@pytest.mark.asyncio
async def test_mcp_server_tools_registered():
    tools = await mcp.list_tools()
    tool_names = [t.name for t in tools]

    expected_tools = [
        "ask_project",
        "prepare_task_context",
        "prepare_deep_context",
        "get_background_job",
        "impact_analysis",
        "record_decision",
        "record_rule",
        "search_code",
        "find_related_files",
        "review_diff",
    ]

    for tool_name in expected_tools:
        assert tool_name in tool_names, f"Tool '{tool_name}' was not registered on the MCP server."


@pytest.mark.asyncio
async def test_mcp_record_decision_tool():
    # Mock DecisionStore
    import json

    mock_add_decision = AsyncMock(return_value=77)
    with patch("apps.mcp_server.server.DecisionStore.add_decision", mock_add_decision):
        result = await mcp.call_tool(
            "record_decision",
            {
                "title": "Use FastMCP",
                "repo_path": "/indexed/example",
                "description": "Standardize on FastMCP for server implementation",
                "status": "active",
            },
        )
        assert len(result) == 1
        data = json.loads(result[0].text)
        assert data["status"] == "success"
        assert data["decision_id"] == 77
        mock_add_decision.assert_called_once()
        assert mock_add_decision.await_args.kwargs["repo_path"] == "/indexed/example"


@pytest.mark.asyncio
async def test_mcp_record_rule_tool():
    import json

    mock_add_rule = AsyncMock(return_value="brand-logo-source")
    with patch("apps.mcp_server.server.RuleStore.add_rule", mock_add_rule):
        result = await mcp.call_tool(
            "record_rule",
            {
                "name": "Brand logo source",
                "repo_path": "/indexed/example",
                "description": "Use shared brand config.",
                "status": "active",
            },
        )
        assert len(result) == 1
        data = json.loads(result[0].text)
        assert data["status"] == "success"
        assert data["rule_id"] == "brand-logo-source"
        assert mock_add_rule.await_args.kwargs["repo_path"] == "/indexed/example"


@pytest.mark.asyncio
async def test_local_mcp_job_lookup_probes_each_v2_pool():
    from apps.mcp_server import server

    class Queue:
        def __init__(self, _redis, prefix):
            self.prefix = prefix

        async def get_job(self, _job_id):
            return {"id": "job-1", "status": "completed"} if self.prefix.endswith(":deep") else None

    with (
        patch("apps.mcp_server.server.JobQueue", Queue),
        patch("apps.mcp_server.server.settings.BRAIN_WORKER_POOLS_V2_ENABLED", True),
    ):
        result = await server.get_background_job("job-1")

    assert result["pool"] == "deep"
