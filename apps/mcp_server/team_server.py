"""Project-scoped team tools, registered next to the personal tools of the remote server.

Importing this module adds ``team_me``, ``team_activity``, ``team_list_tasks``,
``team_create_task`` and ``team_request_run`` to ``remote_server.mcp``; the
API routes behind them apply live membership and project policy, so a caller
without team access sees the same denials here as over HTTP.
"""
from datetime import datetime
from urllib.parse import urlencode
from uuid import UUID

from apps.mcp_server.protocol import tool_meta
from apps.mcp_server.remote_server import _get, _post, checked_tool, mcp

__all__ = ["mcp", "team_me", "team_activity", "team_list_tasks", "team_create_task", "team_request_run"]


@mcp.tool(**tool_meta("team_me"))
@checked_tool
async def team_me() -> dict:
    """Who am I and what can I touch: identity, team state, admin status, project roles and scopes."""
    return await _get("/team/me")


@mcp.tool(**tool_meta("team_activity"))
@checked_tool
async def team_activity(limit: int = 50, offset: int = 0, project_id: UUID | None = None,
                        repository_id: int | None = None, actor_id: int | None = None,
                        kind: list[str] | None = None, since: datetime | None = None) -> dict:
    """Newest-first team activity you may read; ``kind`` takes families (``decision``) or kinds."""
    params: list[tuple[str, str]] = [("limit", str(limit)), ("offset", str(offset))]
    for name, value in (("project_id", project_id), ("repository_id", repository_id), ("actor_id", actor_id),
                        ("since", since.isoformat() if since else None)):
        if value is not None:
            params.append((name, str(value)))
    params += [("kind", value) for value in kind or []]
    return await _get(f"/team/activity?{urlencode(params)}")


@mcp.tool(**tool_meta("team_list_tasks"))
@checked_tool
async def team_list_tasks(project_id: UUID) -> list | dict:
    return await _get(f"/team/projects/{project_id}/tasks")


@mcp.tool(**tool_meta("team_create_task"))
@checked_tool
async def team_create_task(project_id: UUID, repository_id: int, title: str, goal: str,
                           branch: str, base_sha: str, idempotency_key: str) -> dict:
    return await _post(f"/team/projects/{project_id}/tasks", {
        "repository_id": repository_id, "title": title, "goal": goal,
        "branch": branch, "base_sha": base_sha, "idempotency_key": idempotency_key})


@mcp.tool(**tool_meta("team_request_run"))
@checked_tool
async def team_request_run(project_id: UUID, task_id: UUID, branch: str,
                           permissions: list[str], idempotency_key: str) -> dict:
    return await _post(f"/team/projects/{project_id}/tasks/{task_id}/runs", {
        "branch": branch, "permissions": permissions, "idempotency_key": idempotency_key})
