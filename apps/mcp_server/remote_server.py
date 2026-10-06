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

Run:  BRAIN_API_URL=... BRAIN_API_KEY=... python -m apps.mcp_server.remote_server
"""

from __future__ import annotations

import os
from typing import Any, List, Optional

import httpx
from mcp.server.fastmcp import FastMCP

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

_MISSING_CONFIG = (
    "BRAIN_API_URL and BRAIN_API_KEY must be set. The project .mcp.json expands "
    "them from the environment, so set them where your client launches this server."
)

mcp = FastMCP("project-brain-remote")


def _is_safe_base_url(url: str) -> bool:
    """Allow https anywhere, or plain http only to loopback. Prevents sending the
    API key in cleartext to a remote host via a mis-set BRAIN_API_URL."""
    return url.startswith("https://") or url.startswith(("http://127.0.0.1", "http://localhost"))


async def _post(path: str, payload: dict, timeout: float = 120.0) -> Any:
    if not BASE_URL or not API_KEY:
        return {"error": _MISSING_CONFIG, "status": "failed"}
    if not _is_safe_base_url(BASE_URL):
        return {
            "error": "BRAIN_API_URL must use https (refusing to send the API key over plaintext http)",
            "status": "failed",
        }
    headers = {"X-API-Key": API_KEY, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            r = await client.post(f"{BASE_URL}{path}", json=payload, headers=headers)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            return {"error": f"{path} -> HTTP {e.response.status_code}: {e.response.text[:300]}", "status": "failed"}
        except Exception as e:  # noqa: BLE001
            # The exception TYPE matters more than its message here: httpx timeouts
            # and asyncio cancellations both stringify to "", which produced the
            # useless "/ask request failed: " that hid a latency problem for weeks.
            return {
                "error": f"{path} request failed: {type(e).__name__}: {e}".rstrip(": "),
                "status": "failed",
            }


async def _get(path: str, timeout: float = 30.0) -> Any:
    if not BASE_URL or not API_KEY:
        return {"error": _MISSING_CONFIG, "status": "failed"}
    if not _is_safe_base_url(BASE_URL):
        return {
            "error": "BRAIN_API_URL must use https (refusing to send the API key over plaintext http)",
            "status": "failed",
        }
    headers = {"X-API-Key": API_KEY}
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            response = await client.get(f"{BASE_URL}{path}", headers=headers)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            return {
                "error": (
                    f"{path} -> HTTP {exc.response.status_code}: "
                    f"{exc.response.text[:300]}"
                ),
                "status": "failed",
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "error": (
                    f"{path} request failed: {type(exc).__name__}: {exc}"
                ).rstrip(": "),
                "status": "failed",
            }


@mcp.tool()
async def ask_project(query: str, repo_path: Optional[str] = None) -> str:
    """Grounded answer; use search_code first when only location is unknown."""
    res = await _post("/ask", {"query": query, "repo_path": repo_path or DEFAULT_REPO}, timeout=24.0)
    if isinstance(res, dict) and "answer" in res:
        return res["answer"]
    return str(res)


@mcp.tool()
async def prepare_task_context(task_description: str, repo_path: Optional[str] = None) -> dict:
    """Bounded ephemeral code slices after a locator; no durable write by default."""
    return await _post(
        "/context",
        {"task_description": task_description, "repo_path": repo_path or DEFAULT_REPO, "persist": False, "max_tokens": 3500},
        timeout=9.0,
    )


@mcp.tool()
async def prepare_deep_context(
    task_description: str,
    repo_path: Optional[str] = None,
    notify: bool = True,
) -> dict:
    """Queue a quality-first context pack that uses exact CPU LFM reranking."""
    return await _post(
        "/jobs/deep-context",
        {
            "task_description": task_description,
            "repo_path": repo_path or DEFAULT_REPO,
            "notify": notify,
        },
    )


@mcp.tool()
async def get_background_job(job_id: str) -> dict:
    """Read a compact async-job status; full artifacts are fetched explicitly."""
    return await _get(f"/jobs/{job_id}?include_result=false")


@mcp.tool()
async def impact_analysis(change_request: str, repo_path: Optional[str] = None) -> dict:
    """Bounded deterministic impact projection; narrative work stays async."""
    return await _post(
        "/impact",
        {"change_request": change_request, "repo_path": repo_path or DEFAULT_REPO},
        timeout=11.0,
    )


@mcp.tool()
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
) -> dict:
    """Record an architectural/design decision into the Brain's persistent memory."""
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


@mcp.tool()
async def record_rule(
    name: str,
    repo_path: Optional[str] = None,
    description: Optional[str] = None,
    type: Optional[str] = "architecture",
    severity: Optional[str] = "medium",
    status: Optional[str] = "active",
    applies_to: Optional[dict[str, Any]] = None,
    rule_id: Optional[str] = None,
) -> dict:
    """Record a repository-scoped normative rule in the Brain's persistent memory."""
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


@mcp.tool()
async def search_code(query: str, repo_path: Optional[str] = None) -> dict:
    """Compact locator: paths, symbols and ranges only (≤600 estimated tokens)."""
    # The API owns the rollout flag and its locator defaults. Keep this MCP
    # request compatible with pre-v2 deployments that do not know the new
    # optional fields yet.
    return await _post("/search", {"query": query, "limit": 5, "repo_path": repo_path or DEFAULT_REPO})


@mcp.tool()
async def find_related_files(file_path: str, repo_path: Optional[str] = None) -> dict:
    """Find files directly and indirectly related to a path via the dependency graph."""
    return await _post(
        "/related",
        {"file_path": file_path, "repo_path": repo_path or DEFAULT_REPO},
    )


@mcp.tool()
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


@mcp.tool()
async def get_diff_review(job_id: str) -> dict:
    """Poll a diff review queued by ``review_diff``; ``status`` is completed, failed, or still running."""
    return await _get(f"/diff-review/{job_id}")


if __name__ == "__main__":
    mcp.run()
