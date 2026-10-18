"""Project-scoped team tools, registered next to the personal tools of the remote server.

Importing this module adds ``team_me``, ``team_activity``, ``team_list_tasks``,
``team_create_task``, ``team_request_run`` and the governed-memory tools
(``team_propose_decision``, ``team_propose_rule``, ``team_list_proposals``,
``team_approve_proposal``, ``team_reject_proposal``) and the coordination tools
(read: ``team_task_plan``, ``team_coordination_overlaps``, ``team_list_leases``,
``team_integration_queue``; write: ``team_claim_lease``, ``team_renew_lease``,
``team_release_lease``, ``team_declare_scope``, ``team_enqueue_integration``) to
``remote_server.mcp``. Every tool calls the HTTP API route, so live membership,
project policy and ``authorize()`` apply: a caller without team access sees the
same denials here as over HTTP.
"""
from datetime import datetime
from typing import Optional
from urllib.parse import urlencode
from uuid import UUID

from apps.mcp_server.protocol import tool_meta
from apps.mcp_server.remote_server import _get, _post, _put, checked_tool, mcp

__all__ = ["mcp", "team_me", "team_activity", "team_list_tasks", "team_create_task", "team_request_run",
           "team_propose_decision", "team_propose_rule", "team_list_proposals", "team_approve_proposal",
           "team_reject_proposal", "team_task_plan", "team_coordination_overlaps", "team_list_leases",
           "team_integration_queue", "team_claim_lease", "team_renew_lease", "team_release_lease",
           "team_declare_scope", "team_enqueue_integration"]


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


# --- coordination (M3) --------------------------------------------------------------------------
# Reads, then the member-side writes: claim/renew/release a lease, declare a task's scope, queue a
# run. Creating subtasks and integrating or rejecting queue entries stay on the HTTP API and the
# web Board (docs/TEAM.md#team-coordination-m3).


@mcp.tool(**tool_meta("team_task_plan"))
@checked_tool
async def team_task_plan(project_id: UUID, task_id: UUID) -> dict:
    """A task's subtasks with dependency states (ready, waiting, running, done) and the ready set."""
    return await _get(f"/team/projects/{project_id}/tasks/{task_id}/plan")


@mcp.tool(**tool_meta("team_coordination_overlaps"))
@checked_tool
async def team_coordination_overlaps(project_id: UUID, repository_id: int | None = None) -> dict:
    """Declared-scope overlaps among active tasks and with live leases (a risk signal, not a conflict check)."""
    query = f"?{urlencode({'repository_id': repository_id})}" if repository_id is not None else ""
    return await _get(f"/team/projects/{project_id}/overlaps{query}")


@mcp.tool(**tool_meta("team_list_leases"))
@checked_tool
async def team_list_leases(project_id: UUID, repository_id: int | None = None, include_inactive: bool = False) -> dict:
    """Critical leases on a project: resource, holder, fencing token and expiry."""
    params: dict[str, str] = {"include_inactive": str(include_inactive).lower()}
    if repository_id is not None:
        params["repository_id"] = str(repository_id)
    return await _get(f"/team/projects/{project_id}/leases?{urlencode(params)}")


@mcp.tool(**tool_meta("team_integration_queue"))
@checked_tool
async def team_integration_queue(project_id: UUID, repository_id: int | None = None,
                                 include_done: bool = False) -> dict:
    """The integration queue in order, with each entry's last re-validation result."""
    params: dict[str, str] = {"include_done": str(include_done).lower()}
    if repository_id is not None:
        params["repository_id"] = str(repository_id)
    return await _get(f"/team/projects/{project_id}/integration?{urlencode(params)}")


@mcp.tool(**tool_meta("team_claim_lease"))
@checked_tool
async def team_claim_lease(project_id: UUID, repository_id: int, resource: str, task_id: UUID | None = None,
                           ttl_seconds: int = 300) -> dict:
    """Claim a fenced lease on a path or ``resource:<name>``; carry the returned fencing token on every write."""
    return await _post(f"/team/projects/{project_id}/leases", {
        "repository_id": repository_id, "resource": resource,
        "task_id": str(task_id) if task_id else None, "ttl_seconds": ttl_seconds})


@mcp.tool(**tool_meta("team_renew_lease"))
@checked_tool
async def team_renew_lease(project_id: UUID, lease_id: int, fencing_token: int, ttl_seconds: int = 300) -> dict:
    """Extend a lease you hold; refused once it expired or another holder took the path."""
    return await _post(f"/team/projects/{project_id}/leases/{lease_id}/renew",
                       {"fencing_token": fencing_token, "ttl_seconds": ttl_seconds})


@mcp.tool(**tool_meta("team_release_lease"))
@checked_tool
async def team_release_lease(project_id: UUID, lease_id: int, fencing_token: int) -> dict:
    """Release a lease you hold so others can claim the path."""
    return await _post(f"/team/projects/{project_id}/leases/{lease_id}/release", {"fencing_token": fencing_token})


@mcp.tool(**tool_meta("team_declare_scope"))
@checked_tool
async def team_declare_scope(project_id: UUID, task_id: UUID, scope_paths: list[str], expected_version: int) -> dict:
    """Declare the paths a task will touch (overlap warnings use them); ``expected_version`` is the plan version."""
    return await _put(f"/team/projects/{project_id}/tasks/{task_id}/plan",
                      {"scope_paths": scope_paths, "expected_version": expected_version})


@mcp.tool(**tool_meta("team_enqueue_integration"))
@checked_tool
async def team_enqueue_integration(project_id: UUID, run_id: UUID) -> dict:
    """Queue a run that reported its head revision for integration; a reviewer integrates in order."""
    return await _post(f"/team/projects/{project_id}/integration", {"run_id": str(run_id)})
