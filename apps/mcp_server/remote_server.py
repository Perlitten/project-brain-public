"""Project Brain - remote (HTTP-backed) MCP server.

Exposes the same 10 tools as ``server.py`` but talks to a deployed Brain over its
HTTP API instead of the local database. This lets Cursor / Claude use a deployed
Brain as memory from any machine - no local datastores needed.

Configure via environment:
    BRAIN_API_URL   e.g. https://brain.<server-ip>.nip.io   (required)
    BRAIN_API_KEY   an API credential for that deployment (required). Prefer a
                    scoped ``pbk_`` key over the shared PROJECT_BRAIN_API_KEY —
                    this server's tools need ``core:write,jobs:read``
                    (mint: scripts/mint_api_credential.py --name <agent>
                    --scopes core:write,jobs:read), so a leaked or retired
                    agent key is revocable without rotating the shared key.
    BRAIN_REPO      default repo_path for context, search, and ask calls
                    (defaults to TARGET_REPO_PATH or ".")
    BRAIN_MCP_INLINE_MAX_BYTES
                    inline budget for enumeration-shaped results before the tail
                    moves to a ``brain://payload/<token>`` resource (0 disables).

Run:  BRAIN_API_URL=... BRAIN_API_KEY=... python -m apps.mcp_server.remote_server
"""

from __future__ import annotations

import asyncio
import json
import os
from functools import wraps
from typing import Any, List, Optional
from urllib.parse import quote

import httpx
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import InputRequiredResult
from mcp.server.auth.middleware.auth_context import get_access_token

from apps.mcp_server.client_activity import observe_client_activity
from apps.mcp_server.deferred_payload import (
    DeferredPayloadStore,
    compact_payload,
    register_payload_resource,
)
from apps.mcp_server.protocol import (
    confirm_memory_write,
    only_active,
    register_memory_resources,
    register_prompts,
    sample_answer,
    tool_meta,
)


def _env(name: str) -> str:
    """Read an env var, treating an unexpanded ``${VAR}`` placeholder as unset.

    The project-scoped ``.mcp.json`` passes these values through ``${VAR}``
    expansion. When the variable is missing, Claude Code does not fail the
    server: it forwards the literal ``${VAR}`` text. Without this guard that
    text would travel on as the API key and come back as an opaque HTTP 401,
    which reads like a broken deployment rather than a missing variable.
    """
    value = os.environ.get(name, "").strip()
    if value.startswith("${") and value.endswith("}"):
        return ""
    return value


BASE_URL = _env("BRAIN_API_URL").rstrip("/")
API_KEY = _env("BRAIN_API_KEY")
DEFAULT_REPO = _env("BRAIN_REPO") or _env("TARGET_REPO_PATH") or "."

_SAMPLING_SYSTEM = (
    "You answer questions about a software project using only the context provided. "
    "Cite file paths. Say so when the context does not contain the answer."
)

_MISSING_CONFIG = (
    "BRAIN_API_URL and BRAIN_API_KEY must be set. The project .mcp.json expands "
    "them from the environment, so set them where your client launches this server."
)

mcp = MCPServer("project-brain-remote", middleware=[observe_client_activity])

# Enumeration-shaped results (impact projections, graph walks, review reports)
# keep their head inline and park the tail here; see deferred_payload.py.
_PAYLOADS = DeferredPayloadStore()
register_payload_resource(mcp, _PAYLOADS)


def checked_tool(function):
    """Signal operational bridge failures through the MCP tool contract."""
    @wraps(function)
    async def checked(*args, **kwargs):
        result = await function(*args, **kwargs)
        if isinstance(result, dict) and result.get("_bridge_error") is True:
            raise ToolError(str(result["error"]))
        return result
    return checked


@mcp.tool(**tool_meta("record_task_lesson"))
@checked_tool
async def record_task_lesson(task_id: str, statement: str, category: str, validation_id: int,
                             artifact_ids: list[int], source_refs: list[str], idempotency_key: str) -> dict:
    """Capture a lesson from an existing task's reported check and artifacts.

    Requires harness:write; creates L1 evidence only. Consolidation leaves an
    L2 candidate for human approval, never silently makes this claim L3 truth.
    """
    return await _post(f"/harness/tasks/{quote(task_id, safe='')}/lessons", {
        "statement": statement, "category": category, "validation_id": validation_id,
        "artifact_ids": artifact_ids, "source_refs": source_refs, "idempotency_key": idempotency_key,
    })


@mcp.tool(**tool_meta("record_task_observation"))
@checked_tool
async def record_task_observation(task_id: str, statement: str, category: str, idempotency_key: str,
                                  outcome: str = "learning") -> dict:
    """Capture an unvalidated observation for a task; no validation or artifact needed.

    Requires harness:write; creates L1 evidence only. Consolidation needs more
    independent sightings of an unvalidated claim before it becomes L3.
    """
    return await _post(f"/harness/tasks/{quote(task_id, safe='')}/observations", {
        "statement": statement, "category": category, "idempotency_key": idempotency_key, "outcome": outcome,
    })


@mcp.tool(**tool_meta("record_procedure_outcome"))
@checked_tool
async def record_procedure_outcome(skill_id: int, task_id: str, validation_id: int,
                                    outcome: str, artifact_ids: list[int], source_refs: list[str]) -> dict:
    """Record success/failure after applying a skill; one durable result per task.

    Reported receipts must belong to the task. Reading/matching skills is not
    usage and never updates these counters.
    """
    return await _post(f"/skills/{skill_id}/outcomes", {"task_id": task_id,
        "validation_id": validation_id, "outcome": outcome,
        "evidence": {"artifact_ids": artifact_ids, "source_refs": source_refs}})


def _is_safe_base_url(url: str) -> bool:
    """Allow https anywhere, or plain http only to loopback. Prevents sending the
    API key in cleartext to a remote host via a mis-set BRAIN_API_URL."""
    from ipaddress import ip_address
    from urllib.parse import urlsplit
    try:
        parsed = urlsplit(url)
        if not parsed.hostname or parsed.username is not None or parsed.password is not None:
            return False
        _ = parsed.port  # Validate malformed/out-of-range ports before sending a key.
        if parsed.scheme == "https":
            return True
        if parsed.scheme != "http":
            return False
        if parsed.hostname == "localhost":
            return True
        return ip_address(parsed.hostname).is_loopback
    except ValueError:
        return False


# --- backend ---------------------------------------------------------------------
#
# Two deployments share these tools. Launched by a client over stdio, the server
# is a bridge: it calls a Brain over HTTPS with the operator's configured key.
# Mounted inside the API at /mcp (apps/api/mcp_http.py), it calls that same API
# in-process and presents the MCP caller's own credential, so scope checks,
# audit and telemetry see the agent that asked - never a key of the server's.

_IN_PROCESS_BASE = "http://brain.internal"
_asgi_app: Any = None


def use_asgi_backend(app: Any) -> None:
    """Route tool calls to ``app`` in-process instead of to BRAIN_API_URL."""
    global _asgi_app
    _asgi_app = app


def _caller_credential() -> str | None:
    """The credential the Streamable HTTP caller authenticated with, if any.

    ``None`` means there is no authenticated HTTP caller; an empty string is the
    API's dev-open principal (no key configured), which needs no header.
    """
    access = get_access_token()
    return access.token if access is not None else None


def _failure(path: str, exc: Exception) -> dict:
    if isinstance(exc, httpx.HTTPStatusError):
        return _bridge_failure(f"{path} -> HTTP {exc.response.status_code}")
    # The exception TYPE matters more than its message here: httpx timeouts
    # and asyncio cancellations both stringify to "", which produced the
    # useless "/ask request failed: " that hid a latency problem for weeks.
    return _bridge_failure(f"{path} request failed: {type(exc).__name__}")


def _bridge_failure(error: str) -> dict:
    # Polling endpoints may successfully report a failed domain job. Only
    # locally generated request/configuration failures carry this marker.
    return {"error": error, "status": "failed", "_bridge_error": True}


async def _request(method: str, path: str, payload: dict | None, timeout: float) -> Any:
    if _asgi_app is not None:
        credential = _caller_credential()
        if credential is None:
            return _bridge_failure("no authenticated MCP caller for an in-process call")
        headers = {"X-API-Key": credential} if credential else {}
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=_asgi_app), base_url=_IN_PROCESS_BASE)
    else:
        if not BASE_URL or not API_KEY:
            return _bridge_failure(_MISSING_CONFIG)
        if not _is_safe_base_url(BASE_URL):
            return _bridge_failure("BRAIN_API_URL must use https (refusing to send the API key over plaintext http)")
        headers = {"X-API-Key": API_KEY}
        client = httpx.AsyncClient(base_url=BASE_URL, timeout=timeout)
    async with client:
        try:
            # ASGITransport ignores httpx timeouts, so bound the call itself.
            response = await asyncio.wait_for(client.request(method, path, json=payload, headers=headers), timeout)
            response.raise_for_status()
            return response.json()
        except Exception as exc:  # noqa: BLE001
            return _failure(path, exc)


async def _post(path: str, payload: dict, timeout: float = 120.0) -> Any:
    return await _request("POST", path, payload, timeout)


async def _get(path: str, timeout: float = 30.0) -> Any:
    return await _request("GET", path, None, timeout)


# --- resources and prompts ---------------------------------------------------------


async def _active_rules() -> list[dict[str, Any]]:
    return only_active(await _get("/rules"))


async def _active_decisions() -> list[dict[str, Any]]:
    return only_active(await _get("/decisions"))


register_memory_resources(mcp, list_rules=_active_rules, list_decisions=_active_decisions)
register_prompts(mcp)


# --- tools ---------------------------------------------------------------------------


@mcp.tool(**tool_meta("ask_project"))
@checked_tool
async def ask_project(query: str, repo_path: Optional[str] = None, ctx: Optional[Context] = None) -> str | InputRequiredResult:
    """Grounded answer; use search_code first when only location is unknown."""
    repo = repo_path or DEFAULT_REPO
    res = await _post("/ask", {"query": query, "repo_path": repo}, timeout=30.0)
    if isinstance(res, dict) and "answer" in res:
        return res["answer"]
    # The server-side model failed or timed out. The bounded context pack does
    # not need it, so let the client's own model answer from that instead.
    context = await _post(
        "/context",
        {"task_description": query, "repo_path": repo, "persist": False, "max_tokens": 3500},
        timeout=9.0,
    )
    if isinstance(context, dict) and context.get("status") != "failed":
        answer = await sample_answer(
            ctx,
            f"Question: {query}\n\nProject context (JSON):\n{json.dumps(context, default=str)[:24_000]}",
            system=_SAMPLING_SYSTEM,
        )
        if answer is not None:
            return answer
    if isinstance(res, dict) and res.get("status") == "failed":
        raise ToolError(str(res.get("error", "Brain request failed")))
    return str(res)


@mcp.tool(**tool_meta("prepare_task_context"))
@checked_tool
async def prepare_task_context(task_description: str, repo_path: Optional[str] = None) -> dict:
    """Bounded ephemeral code slices after a locator; no durable write by default."""
    return await _post(
        "/context",
        {"task_description": task_description, "repo_path": repo_path or DEFAULT_REPO, "persist": False, "max_tokens": 3500},
        timeout=9.0,
    )


@mcp.tool(**tool_meta("prepare_deep_context"))
@checked_tool
async def prepare_deep_context(
    task_description: str,
    repo_path: Optional[str] = None,
    notify: bool = True,
) -> dict:
    """Queue a durable, quality-first context pack (late-interaction reranking when configured)."""
    return await _post(
        "/jobs/deep-context",
        {
            "task_description": task_description,
            "repo_path": repo_path or DEFAULT_REPO,
            "notify": notify,
        },
    )


@mcp.tool(**tool_meta("get_background_job"))
@checked_tool
async def get_background_job(job_id: str) -> dict:
    """Read a compact async-job status; full artifacts are fetched explicitly."""
    return await _get(f"/jobs/{job_id}?include_result=false")


@mcp.tool(**tool_meta("impact_analysis"))
@checked_tool
async def impact_analysis(change_request: str, repo_path: Optional[str] = None) -> dict:
    """Bounded deterministic impact projection; long tails move to a brain:// resource."""
    return compact_payload(
        await _post(
            "/impact",
            {"change_request": change_request, "repo_path": repo_path or DEFAULT_REPO},
            timeout=11.0,
        ),
        _PAYLOADS,
    )


@mcp.tool(**tool_meta("record_decision"))
@checked_tool
async def record_decision(
    title: str,
    repo_path: Optional[str] = None,
    description: Optional[str] = None,
    status: Optional[str] = "active",
    reason: Optional[str] = None,
    consequences: Optional[str] = None,
    affected_features: Optional[List[str]] = None,
    affected_modules: Optional[List[str]] = None,
    affected_files: Optional[List[str]] = None,
    ctx: Optional[Context] = None,
) -> dict | InputRequiredResult:
    """Record an architectural/design decision into the Brain's persistent memory."""
    declined = await confirm_memory_write(ctx, f"Record decision: {title}. {description or ''}".strip())
    if declined is not None:
        return declined
    return await _post(
        "/decisions",
        {
            "title": title,
            "repo_path": repo_path,
            "description": description,
            "status": status,
            "reason": reason,
            "consequences": consequences,
            "affected_features": affected_features,
            "affected_modules": affected_modules,
            "affected_files": affected_files,
        },
    )


@mcp.tool(**tool_meta("record_rule"))
@checked_tool
async def record_rule(
    name: str,
    repo_path: Optional[str] = None,
    description: Optional[str] = None,
    type: Optional[str] = "architecture",
    severity: Optional[str] = "medium",
    status: Optional[str] = "active",
    applies_to: Optional[dict[str, Any]] = None,
    rule_id: Optional[str] = None,
    ctx: Optional[Context] = None,
) -> dict | InputRequiredResult:
    """Record a repository-scoped normative rule in the Brain's persistent memory."""
    declined = await confirm_memory_write(ctx, f"Record {severity} rule: {name}. {description or ''}".strip())
    if declined is not None:
        return declined
    return await _post(
        "/rules",
        {
            "name": name,
            "repo_path": repo_path,
            "description": description,
            "type": type,
            "severity": severity,
            "status": status,
            "applies_to": applies_to,
            "rule_id": rule_id,
        },
    )


@mcp.tool(**tool_meta("search_code"))
@checked_tool
async def search_code(query: str, repo_path: Optional[str] = None) -> dict:
    """Compact locator: paths, symbols and ranges only (≤600 estimated tokens)."""
    # The API owns the rollout flag and its locator defaults. Keep this MCP
    # request compatible with pre-v2 deployments that do not know the new
    # optional fields yet.
    return await _post("/search", {"query": query, "limit": 5, "repo_path": repo_path or DEFAULT_REPO})


@mcp.tool(**tool_meta("find_related_files"))
@checked_tool
async def find_related_files(file_path: str, repo_path: Optional[str] = None) -> dict:
    """Graph neighbours of a path; long tails move to a brain:// resource."""
    return compact_payload(
        await _post(
            "/related",
            {"file_path": file_path, "repo_path": repo_path or DEFAULT_REPO},
        ),
        _PAYLOADS,
    )


@mcp.tool(**tool_meta("review_diff"))
@checked_tool
async def review_diff(
    base: Optional[str] = None,
    head: Optional[str] = "current",
    repo_path: Optional[str] = None,
) -> dict:
    """Queue a diff review; omitted base resolves from that repository's origin/HEAD.

    Returns ``{"status": "processing", "job_id": ...}``. Poll ``get_diff_review(job_id)``
    for the report.
    """
    return await _post(
        "/diff-review",
        {
            "base": base,
            "head": head,
            "repo_path": repo_path or DEFAULT_REPO,
        },
    )


@mcp.tool(**tool_meta("get_diff_review"))
@checked_tool
async def get_diff_review(job_id: str) -> dict:
    """Poll a diff review queued by ``review_diff``; ``status`` is completed, failed, or still running.

    A completed report is the one result that is routinely larger than the
    decision it informs, so its tail moves to a ``brain://payload`` resource.
    """
    return compact_payload(
        await _get(f"/diff-review/{quote(job_id, safe='')}"),
        _PAYLOADS,
    )


if __name__ == "__main__":
    from brain.config.settings import settings

    if settings.BRAIN_TEAM_MODE:
        raise RuntimeError("Personal remote MCP is unavailable in team mode; use authenticated native MCP")
    mcp.run()
