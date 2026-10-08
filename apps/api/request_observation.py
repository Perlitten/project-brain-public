"""Observe handler outcomes without persisting prompts or response payloads."""
from __future__ import annotations

import asyncio
import time
from typing import Any

from loguru import logger


def classify_result(result: Any, operation: str) -> str:
    if not isinstance(result, dict):
        return "success" if result else "empty"
    status = str(result.get("status", "")).lower()
    if result.get("error") or status in {"failed", "error"}:
        return "error"
    degraded = result.get("degraded") or []
    if any(str(reason).startswith("locator_error:") for reason in degraded):
        return "error"
    if status in {"partial", "abstained"} or result.get("critic_status") in {"PARTIAL_CONTEXT", "LOW_CONFIDENCE"}:
        return "partial"
    if degraded:
        return "partial"
    if operation == "search":
        return "success" if any(result.get(key) for key in ("results", "files", "symbols", "chunks")) else "empty"
    return "success" if any(result.get(key) for key in ("slices", "markdown_content", "path", "context")) else "empty"


def observed_repository_path(result: Any, fallback: str | None) -> str | None:
    if isinstance(result, dict):
        for key in ("repo", "repository", "repository_scope"):
            repo = result.get(key)
            if isinstance(repo, dict):
                path = repo.get("path") or repo.get("repository_path") or repo.get("requested_path")
                if isinstance(path, str) and path:
                    return path
    return str(fallback) if fallback else None


async def observe_http_request(request: Any, *, operation: str, result: Any, repo_path: str | None,
                               outcome: str, started: float) -> None:
    latency_ms = (time.perf_counter() - started) * 1000
    state = getattr(request, "state", None)
    principal = getattr(state, "principal", None)
    path = observed_repository_path(result, repo_path)

    async def persist() -> None:
        from apps.api.telemetry import record_request, repository_id_for_path
        await record_request(operation=operation, repository_id=await repository_id_for_path(path),
                             repository_path=path, principal_name=getattr(principal, "name", "api:unattributed"),
                             request_id=getattr(state, "request_id", None), outcome=outcome,
                             latency_ms=latency_ms)
    try:
        await asyncio.wait_for(persist(), timeout=0.5)
    except Exception as exc:
        logger.warning("Request measurement skipped: {}", type(exc).__name__)
