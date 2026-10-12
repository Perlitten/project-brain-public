"""MCP runtime/status introspection helpers.

These functions inspect the MCP server object itself (``apps.mcp_server``),
so they live on the apps side of the layer boundary. Re-exported through
``apps.api.helpers`` for existing callers.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List

from loguru import logger

from brain.config.settings import settings

def get_mcp_tools_list() -> List[Dict[str, Any]]:
    mcp_tools = []
    try:
        from apps.mcp_server.server import mcp as mcp_instance

        tool_manager = mcp_instance._tool_manager
        for tool_obj in tool_manager.list_tools():
            tool_name = tool_obj.name
            tool_desc = getattr(tool_obj, "description", "") or ""
            if not tool_desc:
                tool_fn = getattr(tool_obj, "fn", None)
                if tool_fn is not None:
                    tool_desc = str(tool_fn.__doc__ or "")
            schema_dict = {}
            if hasattr(tool_obj, "parameters") and tool_obj.parameters:
                schema_dict = tool_obj.parameters
            elif hasattr(tool_obj, "input_model") and tool_obj.input_model:
                try:
                    schema_dict = tool_obj.input_model.model_json_schema()
                except Exception:
                    schema_dict = {}
            mcp_tools.append({"name": tool_name, "description": tool_desc.strip(), "schema": schema_dict})
    except Exception as exc:
        logger.warning(f"Failed to inspect MCP tools: {exc}")
    return mcp_tools




async def get_mcp_runtime_status() -> Dict[str, Any]:
    """Cheap MCP liveness: tool registration plus datastore connectivity."""
    from time import perf_counter

    started = perf_counter()
    tools = get_mcp_tools_list()
    registration_ms = round((perf_counter() - started) * 1000, 2)
    if not tools:
        return {
            "server_status": "unavailable",
            "status": "unavailable",
            "available": False,
            "tools": [],
            "total_tools": 0,
            "diagnostics": {
                "available": False,
                "error": "MCP tool surface is empty",
                "probe_status": "skipped",
                "probe_target": "registration_and_datastores",
                "stage_timings_ms": {
                    "registration": registration_ms,
                    "datastores": 0.0,
                    "total": registration_ms,
                },
            },
        }

    datastore_started = perf_counter()
    datastore_error = None
    datastore_status: Dict[str, str] = {}
    try:
        from brain.database.session import check_health as check_datastore_health

        services = await asyncio.wait_for(check_datastore_health(), timeout=5.0)
        datastore_status = {
            name: str((services.get(name) or {}).get("status") or "unavailable")
            for name in ("postgres", "redis", "neo4j")
        }
    except asyncio.TimeoutError:
        datastore_error = "Datastore liveness check timed out"
    except Exception as exc:
        datastore_error = str(exc)

    datastores_ms = round((perf_counter() - datastore_started) * 1000, 2)
    unhealthy = [name for name, status in datastore_status.items() if status != "healthy"]
    probe_ok = datastore_error is None and not unhealthy
    status = "online" if probe_ok else "degraded"
    if datastore_error is None and unhealthy:
        datastore_error = f"Unhealthy MCP datastores: {', '.join(unhealthy)}"
    return {
        "server_status": status,
        "status": status,
        "available": probe_ok,
        "tools": tools,
        "total_tools": len(tools),
        "diagnostics": {
            "available": True,
            "error": datastore_error,
            "probe_target": "registration_and_datastores",
            "probe_payload": {"datastores": datastore_status},
            "probe_status": "passed" if probe_ok else "failed",
            "stage_timings_ms": {
                "registration": registration_ms,
                "datastores": datastores_ms,
                "total": round((perf_counter() - started) * 1000, 2),
            },
        },
    }


async def run_mcp_self_check(timeout_seconds: float = 30.0) -> Dict[str, Any]:
    """Run the MCP self-check and record its outcome against the current
    configuration fingerprint. The checklist displays only this recorded,
    labeled result — and invalidates it when the config it ran against
    changes."""
    from brain.onboarding.readiness import config_fingerprint
    from brain.onboarding.setup_state import record_self_check

    fingerprint = config_fingerprint()
    runtime = await get_mcp_readiness_status(timeout_seconds)
    record_self_check(
        str(runtime.get("readiness_status") or "failed"),
        fingerprint,
        {
            "retrieval_probe": (runtime.get("retrieval") or {}).get("probe_status"),
            "stage_timings_ms": runtime.get("stage_timings_ms") or {},
            "error": (runtime.get("retrieval") or {}).get("error"),
        },
    )
    return runtime


async def get_mcp_readiness_status(timeout_seconds: float = 30.0) -> Dict[str, Any]:
    """Full MCP readiness with real retrieval, isolated from dashboard liveness."""
    from time import perf_counter

    started = perf_counter()
    runtime = await get_mcp_runtime_status()
    timings = dict((runtime.get("diagnostics") or {}).get("stage_timings_ms") or {})
    if not runtime.get("available"):
        timings["retrieval"] = 0.0
        timings["total"] = round((perf_counter() - started) * 1000, 2)
        return {
            "readiness_status": "degraded",
            "runtime": runtime,
            "retrieval": {"probe_status": "skipped", "error": "MCP runtime liveness failed"},
            "stage_timings_ms": timings,
        }

    retrieval_started = perf_counter()
    retrieval_error = None
    retrieval_payload: Dict[str, Any] = {}
    from brain.config.paths import resolve_repo_path

    probe_repo = str(resolve_repo_path(settings.TARGET_REPO_PATH))
    try:
        from apps.mcp_server import server as mcp_server

        probe_target = getattr(mcp_server, "search_code", None)
        if not callable(probe_target):
            retrieval_error = "search_code tool entrypoint is not callable"
        else:
            payload = await asyncio.wait_for(
                probe_target("brain readiness probe", repo_path=probe_repo),
                timeout=max(1.0, min(float(timeout_seconds), 120.0)),
            )
            if isinstance(payload, dict):
                retrieval_payload = {
                    key: payload[key]
                    for key in ("status", "error", "retrieval_debug", "repository_scope")
                    if key in payload
                }
                if payload.get("error") or payload.get("status") == "failed":
                    retrieval_error = str(payload.get("error") or "Retrieval probe failed")
    except asyncio.TimeoutError:
        retrieval_error = f"Retrieval readiness timed out after {timeout_seconds:g} seconds"
    except Exception as exc:
        retrieval_error = str(exc)

    timings["retrieval"] = round((perf_counter() - retrieval_started) * 1000, 2)
    timings["total"] = round((perf_counter() - started) * 1000, 2)
    return {
        "readiness_status": "ready" if retrieval_error is None else "degraded",
        "runtime": runtime,
        "retrieval": {
            "probe_status": "passed" if retrieval_error is None else "failed",
            "probe_target": "search_code",
            "repo_path": probe_repo,
            "timeout_seconds": timeout_seconds,
            "error": retrieval_error,
            "payload": retrieval_payload,
        },
        "stage_timings_ms": timings,
    }


