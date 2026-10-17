"""Project-scoped team tools, registered next to the personal tools of the remote server.

Importing this module adds ``team_me``, ``team_activity``, ``team_list_tasks``,
``team_create_task``, ``team_request_run`` and the governed-memory tools
(``team_propose_decision``, ``team_propose_rule``, ``team_list_proposals``,
``team_approve_proposal``, ``team_reject_proposal``) to ``remote_server.mcp``;
the API routes behind them apply live membership and project policy, so a
caller without team access sees the same denials here as over HTTP.
"""
from datetime import datetime
from typing import Optional
from urllib.parse import urlencode
from uuid import UUID

from apps.mcp_server.protocol import tool_meta
from apps.mcp_server.remote_server import _get, _post, checked_tool, mcp

__all__ = ["mcp", "team_me", "team_activity", "team_list_tasks", "team_create_task", "team_request_run",
           "team_propose_decision", "team_propose_rule", "team_list_proposals", "team_approve_proposal",
           "team_reject_proposal"]


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


# --- governed memory: proposal -> review -> approved knowledge ---------------------------------
# In team state a member's decision or rule is a draft until an independent reviewer approves it;
# the API routes behind these tools apply the same role checks as over HTTP (docs/MEMORY_REVIEW.md).


@mcp.tool(**tool_meta("team_propose_decision"))
@checked_tool
async def team_propose_decision(title: str, repo_path: Optional[str] = None, description: Optional[str] = None,
                                reason: Optional[str] = None, consequences: Optional[str] = None,
                                expected_version: Optional[int] = None) -> dict:
    """Propose a decision for review; it is not approved knowledge until a reviewer approves it."""
    return await _post("/decisions/proposals", {
        "title": title, "repo_path": repo_path, "description": description, "reason": reason,
        "consequences": consequences, "expected_version": expected_version})


@mcp.tool(**tool_meta("team_propose_rule"))
@checked_tool
async def team_propose_rule(name: str, repo_path: Optional[str] = None, description: Optional[str] = None,
                            severity: Optional[str] = "medium", type: Optional[str] = "architecture",
                            rule_id: Optional[str] = None, expected_version: Optional[int] = None) -> dict:
    """Propose a rule for review; it is not enforced or retrieved until a reviewer approves it."""
    return await _post("/rules/proposals", {
        "name": name, "repo_path": repo_path, "description": description, "severity": severity, "type": type,
        "rule_id": rule_id, "expected_version": expected_version})


@mcp.tool(**tool_meta("team_list_proposals"))
@checked_tool
async def team_list_proposals(repo_path: Optional[str] = None, system_global: bool = False,
                              state: str = "pending", mine: bool = False) -> dict:
    """The review queue of one repository (Reviewer), the system-global queue (team administrator), or yours."""
    query: dict[str, str] = {"state": state}
    if mine:
        query["mine"] = "true"
    elif system_global:
        query["scope"] = "system_global"
    elif repo_path:
        query["repo_path"] = repo_path
    return await _get(f"/memory/proposals?{urlencode(query)}")


@mcp.tool(**tool_meta("team_approve_proposal"))
@checked_tool
async def team_approve_proposal(proposal_id: int, expected_version: int, reason: Optional[str] = None) -> dict:
    """Approve a proposal you did not author; ``expected_version`` is the proposal version you reviewed."""
    return await _post(f"/memory/proposals/{proposal_id}/approve",
                       {"expected_version": expected_version, "reason": reason})


@mcp.tool(**tool_meta("team_reject_proposal"))
@checked_tool
async def team_reject_proposal(proposal_id: int, expected_version: int, reason: Optional[str] = None) -> dict:
    """Reject a proposal you did not author; nothing reaches approved memory."""
    return await _post(f"/memory/proposals/{proposal_id}/reject",
                       {"expected_version": expected_version, "reason": reason})
