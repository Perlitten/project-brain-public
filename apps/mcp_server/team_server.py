"""Project-scoped MCP facade; no personal tools, resources, prompts or model calls."""
from uuid import UUID

from mcp.server.mcpserver import MCPServer

from apps.mcp_server.remote_server import _get, _post, checked_tool

mcp = MCPServer("project-brain-team-foundations")


@mcp.tool()
@checked_tool
async def team_list_tasks(project_id: UUID) -> list | dict:
    return await _get(f"/team/projects/{project_id}/tasks")


@mcp.tool()
@checked_tool
async def team_create_task(project_id: UUID, repository_id: int, title: str, goal: str,
                           branch: str, base_sha: str, idempotency_key: str) -> dict:
    return await _post(f"/team/projects/{project_id}/tasks", {
        "repository_id": repository_id, "title": title, "goal": goal,
        "branch": branch, "base_sha": base_sha, "idempotency_key": idempotency_key})


@mcp.tool()
@checked_tool
async def team_request_run(project_id: UUID, task_id: UUID, branch: str,
                           permissions: list[str], idempotency_key: str) -> dict:
    return await _post(f"/team/projects/{project_id}/tasks/{task_id}/runs", {
        "branch": branch, "permissions": permissions, "idempotency_key": idempotency_key})
