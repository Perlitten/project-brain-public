#!/usr/bin/env python3
"""Spawn MCP server over stdio, verify all tools, and exercise core transports."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client

PROJECT_ROOT = Path(__file__).resolve().parents[1]

REQUIRED_TOOLS = {
    "ask_project",
    "prepare_task_context",
    "impact_analysis",
    "record_decision",
    "record_rule",
    "search_code",
    "find_related_files",
    "review_diff",
}

TOOL_CALLS = [
    ("search_code", {"query": "dashboard router"}),
    ("review_diff", {"base": "HEAD", "head": "HEAD"}),
    ("ask_project", {"query": "What is Project Brain?"}),
    (
        "prepare_task_context",
        {
            "task_description": "Read apps/api/main.py entrypoint",
            "repo_path": str(PROJECT_ROOT),
        },
    ),
    ("impact_analysis", {"change_request": "Add health widget to dashboard"}),
    (
        "record_decision",
        {
            "title": "MCP stdio E2E probe",
            "description": "Automated stdio transport verification",
            "status": "test",
        },
    ),
    ("find_related_files", {"file_path": "apps/api/main.py"}),
]

TOOL_TIMEOUT_S = 120


async def _call_tool(session: ClientSession, tool: str, args: dict) -> tuple[bool, str | None]:
    with anyio.move_on_after(TOOL_TIMEOUT_S) as scope:
        resp = await session.call_tool(tool, args)
    if scope.cancelled_caught:
        return False, f"timeout after {TOOL_TIMEOUT_S}s"
    ok = not resp.isError
    payload = None
    if resp.content:
        block = resp.content[0]
        payload = getattr(block, "text", str(block))[:200]
    if not ok:
        return False, payload
    return True, "ok"


async def _run() -> dict:
    env = get_default_environment()
    env.update(os.environ)
    env["DEBUG"] = "false"
    # Force mock providers so stdio E2E does not depend on external LLM APIs
    env["DEFAULT_LLM_PROVIDER"] = "mock"
    env["DEFAULT_EMBEDDING_PROVIDER"] = "mock"
    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "apps.mcp_server.server"],
        cwd=str(PROJECT_ROOT),
        env=env,
    )
    results: dict = {"steps": [], "pass": True}

    try:
        async with stdio_client(server_params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                init = await session.initialize()
                results["steps"].append({"initialize": init.serverInfo.model_dump()})

                tools = await session.list_tools()
                tool_names = {t.name for t in tools.tools}
                results["steps"].append({"tools/list": sorted(tool_names)})
                if not REQUIRED_TOOLS.issubset(tool_names):
                    results["pass"] = False
                    results["error"] = f"Missing tools: {sorted(REQUIRED_TOOLS - tool_names)}"
                    return results

                for tool, args in TOOL_CALLS:
                    try:
                        ok, detail = await _call_tool(session, tool, args)
                        results["steps"].append({tool: detail})
                        if not ok:
                            results["pass"] = False
                    except Exception as exc:
                        results["steps"].append({tool: str(exc)})
                        results["pass"] = False
    except Exception as exc:
        results["pass"] = False
        results["transport_error"] = str(exc)

    return results


def main() -> int:
    results = anyio.run(_run)
    out = PROJECT_ROOT / "reports" / "mcp-stdio-e2e.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2))
    return 0 if results["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
