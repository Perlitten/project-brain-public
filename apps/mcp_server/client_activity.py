"""Best-effort MCP connection evidence, shared without loading the local core.

A remote stdio installation may contain only apps/mcp_server and the SDK.
Persistence is optional there; cohosted API installs can record Setup evidence.
"""

from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.types import Implementation
from mcp_types.version import MODERN_PROTOCOL_VERSIONS


def record_client_activity(client_info: Implementation | None) -> None:
    """Record client evidence when the Brain core is installed and writable."""
    try:
        from brain.onboarding.setup_state import record_client_activity as persist

        persist(getattr(client_info, "name", None) or "unknown-client",
                getattr(client_info, "version", "") or "")
    except Exception:
        pass  # Optional observability must never break a remote client.


async def observe_client_activity(ctx: ServerRequestContext, call_next: CallNext) -> HandlerResult:
    """Observe valid handshakes and modern request identities through SDK middleware."""
    result = await call_next(ctx)
    if ctx.protocol_version in MODERN_PROTOCOL_VERSIONS:
        params = ctx.session.client_params
        if params is not None:
            record_client_activity(params.client_info)
    elif ctx.method == "initialize" and ctx.params:
        info = ctx.params.get("clientInfo")
        if info:
            record_client_activity(Implementation.model_validate(info))
    return result


# --------------------------------------------------------------------------- per-tool telemetry

_PENDING: set = set()


def _call_failed(result: object) -> bool:
    """A tool call failed when the SDK flags it, or when Brain's own payload says so."""
    if getattr(result, "is_error", False) or getattr(result, "isError", False):
        return True
    payload = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
    if isinstance(result, dict):
        payload = payload or result.get("structuredContent") or result
    if isinstance(payload, dict):
        nested = payload.get("result")
        inner = nested if isinstance(nested, dict) else payload
        return str(inner.get("status", "")).lower() == "failed" or (
            "error" in inner and not inner.get("status")
        )
    return False


def _record_tool_call(tool: str, surface: str, outcome: str, latency_ms: float) -> None:
    """Persist in the background when the Brain core is installed; never block or raise."""
    try:
        import asyncio

        from brain.telemetry.tool_calls import record_tool_call

        task = asyncio.get_running_loop().create_task(record_tool_call(tool, surface, outcome, latency_ms))
        _PENDING.add(task)
        task.add_done_callback(_PENDING.discard)
    except Exception:
        pass  # A remote-only install has no database: telemetry is optional.


def tool_call_observer(surface: str, enabled=lambda: True):
    """SDK middleware that times every tools/call and records its outcome.

    ``enabled`` lets a server record only where the Brain database is local
    (the remote bridge on an agent's laptop has none; the API it calls does).
    """

    async def observe(ctx: ServerRequestContext, call_next: CallNext) -> HandlerResult:
        if ctx.method != "tools/call" or not enabled():
            return await call_next(ctx)
        import time

        tool = str((ctx.params or {}).get("name") or "unknown")
        started = time.perf_counter()
        outcome = "error"
        try:
            result = await call_next(ctx)
            outcome = "error" if _call_failed(result) else "success"
            return result
        finally:
            _record_tool_call(tool, surface, outcome, (time.perf_counter() - started) * 1000)

    return observe
