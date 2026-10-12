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
