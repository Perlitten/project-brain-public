"""First-use setup API: readiness status, safe config writes, agent verify.

The write surface persists only non-secret keys (``brain.onboarding.envfile``
enforces the allowlist); provider credentials are never accepted, stored, or
echoed by these endpoints.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from apps.api.auth import require_api_key, require_scope
from apps.api.helpers import run_mcp_self_check
from brain.config.settings import settings
from brain.onboarding.envfile import update_env_file
from brain.onboarding.provider_check import verify_configured_providers
from brain.onboarding.readiness import collect_setup_status

router = APIRouter(prefix="/api/setup", tags=["setup"], dependencies=[Depends(require_api_key)])

_PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _mcp_server_launch() -> dict[str, Any]:
    """Launch contract for the MCP server from ANY working directory:
    ``python -m`` with PYTHONPATH pointing at this install (settings load the
    install's .env regardless of cwd). A pip-installed ``brain-mcp`` console
    script — the wheel entry point — is the packaged equivalent."""
    return {
        "command": sys.executable,
        "args": ["-m", "apps.mcp_server.server"],
        "env": {"PYTHONPATH": str(_PROJECT_ROOT)},
    }


def _client_configs() -> dict[str, dict[str, str]]:
    """Copy-paste MCP client configs using each client's real contract
    (``mcpServers`` map, no ``cwd`` key — it is not part of the documented
    contract and silently ignored by clients that don't support it)."""
    server = _mcp_server_launch()
    snippet = json.dumps({"mcpServers": {"brain": server}}, indent=2)
    python_exe = server["command"]
    root = server["env"]["PYTHONPATH"]
    return {
        "Claude Code": {
            "where": "~/.claude.json or project .mcp.json",
            "config": snippet,
            "cli": f'claude mcp add brain --env PYTHONPATH="{root}" -- "{python_exe}" -m apps.mcp_server.server',
        },
        "Cursor": {
            "where": "~/.cursor/mcp.json",
            "config": snippet,
            "cli": "",
        },
        "Any MCP client (stdio)": {
            "where": "your client's server config",
            "config": snippet,
            "cli": f'PYTHONPATH="{root}" {python_exe} -m apps.mcp_server.server'
            f"   # or 'brain-mcp' from a pip install",
        },
    }

_SETTINGS_ATTR = {
    "TARGET_REPO_PATH": "TARGET_REPO_PATH",
    "DEFAULT_LLM_PROVIDER": "DEFAULT_LLM_PROVIDER",
    "DEFAULT_EMBEDDING_PROVIDER": "DEFAULT_EMBEDDING_PROVIDER",
}


class SetupConfigRequest(BaseModel):
    target_repo_path: Optional[str] = None
    default_llm_provider: Optional[str] = None
    default_embedding_provider: Optional[str] = None


@router.get("/status", dependencies=[Depends(require_scope("setup:read"))])
async def setup_status() -> Dict[str, Any]:
    return await collect_setup_status()


@router.post("/config", dependencies=[Depends(require_scope("setup:write"))])
async def update_setup_config(body: SetupConfigRequest) -> Dict[str, Any]:
    updates: Dict[str, str] = {}
    if body.target_repo_path is not None:
        path = Path(body.target_repo_path).expanduser().resolve()
        if not path.is_dir():
            raise HTTPException(
                status_code=400, detail=f"Repository path is not a directory: {path}"
            )
        updates["TARGET_REPO_PATH"] = str(path)
    for field, env_key in (
        ("default_llm_provider", "DEFAULT_LLM_PROVIDER"),
        ("default_embedding_provider", "DEFAULT_EMBEDDING_PROVIDER"),
    ):
        value = getattr(body, field)
        if value is not None:
            updates[env_key] = value.strip().lower()
    try:
        applied = update_env_file(Path(".env"), updates)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    for env_key, value in applied.items():
        setattr(settings, _SETTINGS_ATTR[env_key], value)
    return {"applied": sorted(applied.keys()), "status": await collect_setup_status()}


@router.post("/verify-agent", dependencies=[Depends(require_scope("setup:read"))])
async def verify_agent() -> Dict[str, Any]:
    """Run the server-side MCP self-check and record it (labeled self-check,
    invalidated by config change). This does not — and cannot — prove an
    external client connected; only a recorded initialize handshake does."""
    try:
        runtime = await run_mcp_self_check()
    except Exception as exc:  # pragma: no cover - defensive
        return {"available": False, "error": str(exc)}
    return runtime


@router.post("/verify-provider", dependencies=[Depends(require_scope("setup:write"))])
async def verify_provider() -> Dict[str, Any]:
    """Cheap live probe of each configured provider through the real
    adapters. Records verified/failed per credential fingerprint; masked
    errors only — credentials never appear in the response or state file."""
    return await verify_configured_providers()
