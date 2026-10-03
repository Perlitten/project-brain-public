"""First-use setup API: readiness status, safe config writes, agent verify.

``/config`` persists only non-secret keys (``brain.onboarding.envfile``
enforces the allowlist). ``/provider`` additionally accepts the universal
``LLM_API_KEY`` / ``EMBEDDING_API_KEY`` — write-only: a key is stored, never
returned; responses only say whether one is set.
"""
from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path
from typing import Any, Dict, Literal, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, SecretStr

from apps.api.auth import require_api_key, require_scope
from apps.api.helpers import run_mcp_self_check
from brain.config.settings import settings
from brain.llm.presets import PRESETS, normalize_provider_name, resolve_embedding_endpoint, resolve_llm_endpoint
from brain.onboarding.envfile import SUPPORTED_SETUP_PROVIDERS, setup_env_path, update_env_file
from brain.onboarding.setup_state import state_dir
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
        "env": {
            "PYTHONPATH": str(_PROJECT_ROOT),
            "BRAIN_ENV_FILE": str(setup_env_path().resolve()),
            "BRAIN_SETUP_STATE_DIR": str(state_dir().resolve()),
        },
    }


def _client_configs() -> dict[str, dict[str, str]]:
    """Copy-paste MCP client configs using each client's real contract
    (``mcpServers`` map, no ``cwd`` key — it is not part of the documented
    contract and silently ignored by clients that don't support it)."""
    server = _mcp_server_launch()
    snippet = json.dumps({"mcpServers": {"brain": server}}, indent=2)
    python_exe = server["command"]
    python_arg = shlex.quote(python_exe)
    cli_env_args = " ".join(
        f"--env {shlex.quote(f'{key}={value}')}" for key, value in server["env"].items()
    )
    shell_env = " ".join(f"{key}={shlex.quote(value)}" for key, value in server["env"].items())
    return {
        "Claude Code": {
            "where": "~/.claude.json or project .mcp.json",
            "config": snippet,
            "cli": f"claude mcp add brain {cli_env_args} -- {python_arg} -m apps.mcp_server.server",
        },
        "Cursor": {
            "where": "~/.cursor/mcp.json",
            "config": snippet,
            "cli": "",
        },
        "Any MCP client (stdio)": {
            "where": "your client's server config",
            "config": snippet,
            "cli": f"{shell_env} {python_arg} -m apps.mcp_server.server"
            f"   # or 'brain-mcp' from a pip install",
        },
    }

def _apply_to_settings(applied: Dict[str, str]) -> None:
    """Mirror written keys onto the live settings (blank = unset)."""
    for env_key, value in applied.items():
        if env_key == "EMBEDDING_DIMENSION":
            setattr(settings, env_key, int(value) if value else 0)
        elif env_key in ("TARGET_REPO_PATH", "DEFAULT_LLM_PROVIDER", "DEFAULT_EMBEDDING_PROVIDER"):
            setattr(settings, env_key, value)
        else:
            setattr(settings, env_key, value or None)


class SetupConfigRequest(BaseModel):
    target_repo_path: Optional[str] = None
    default_llm_provider: Optional[str] = None
    default_embedding_provider: Optional[str] = None


class ProviderConfigRequest(BaseModel):
    """One provider slot. Omitted fields are left as they are; an empty
    string clears the override so the preset default applies again."""

    slot: Literal["llm", "embedding"]
    provider: str = Field(min_length=1, max_length=64)
    base_url: Optional[str] = Field(default=None, max_length=512)
    model: Optional[str] = Field(default=None, max_length=200)
    summarizer_model: Optional[str] = Field(default=None, max_length=200)
    dimension: Optional[int] = Field(default=None, ge=0, le=65536)
    api_key: Optional[SecretStr] = None
    clear_api_key: bool = False


def _safe_url(url: Optional[str]) -> Optional[str]:
    """scheme://host[:port]/path — never userinfo, query or fragment."""
    if not url:
        return None
    try:
        p = urlparse(url)
    except ValueError:
        return None
    if not p.hostname:
        return None
    host = f"{p.hostname}:{p.port}" if p.port else p.hostname
    return f"{p.scheme}://{host}{p.path}"


def _provider_view() -> Dict[str, Any]:
    """Current provider configuration — names, URLs and models only; for the
    keys just whether one is set and which setting supplies it."""
    view: Dict[str, Any] = {}
    llm_name = normalize_provider_name(settings.DEFAULT_LLM_PROVIDER or "mock")
    emb_name = normalize_provider_name(settings.DEFAULT_EMBEDDING_PROVIDER or "mock")
    if llm_name in PRESETS:
        ep = resolve_llm_endpoint(llm_name)
        view["llm"] = {
            "provider": llm_name,
            "base_url": _safe_url(ep.base_url),
            "model": ep.model,
            "summarizer_model": (settings.SUMMARIZER_MODEL or None),
            "key_set": bool(ep.api_key),
            "key_source": ep.key_source,
            "requires_key": ep.requires_key,
        }
    else:
        view["llm"] = {"provider": llm_name, "native": True}
    if emb_name in PRESETS:
        ep = resolve_embedding_endpoint(emb_name)
        view["embedding"] = {
            "provider": emb_name,
            "base_url": _safe_url(ep.base_url),
            "model": ep.model,
            "dimension": ep.dimension,
            "key_set": bool(ep.api_key),
            "key_source": ep.key_source,
            "requires_key": ep.requires_key,
        }
    else:
        view["embedding"] = {"provider": emb_name, "native": True}
    return view


@router.get("/status", dependencies=[Depends(require_scope("setup:read"))])
async def setup_status() -> Dict[str, Any]:
    status = await collect_setup_status()
    configs = _client_configs()
    status["client_configs"] = configs
    for step in status.get("steps", []):
        if step.get("id") == "agent":
            step["command"] = configs["Claude Code"]["cli"]
    return status


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
        applied = update_env_file(setup_env_path(), updates)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _apply_to_settings(applied)
    return {"applied": sorted(applied.keys()), "status": await collect_setup_status()}


@router.get("/providers", dependencies=[Depends(require_scope("setup:read"))])
async def provider_presets() -> Dict[str, Any]:
    """Preset catalogue for the provider picker, plus what is configured now."""
    presets = [
        {
            "name": p.name,
            "label": p.label,
            "display_name": p.display_name,
            "base_url": p.base_url,
            "requires_key": p.requires_key,
            "key_env": p.key_env,
            "llm_model": p.llm_model,
            "summarizer_model": p.summarizer_model,
            "embedding_model": p.embedding_model,
            "embedding_dimension": p.embedding_dimension,
            "embeddings": p.name in SUPPORTED_SETUP_PROVIDERS["DEFAULT_EMBEDDING_PROVIDER"],
        }
        for p in PRESETS.values()
    ]
    return {"presets": presets, "current": _provider_view()}


@router.post("/provider", dependencies=[Depends(require_scope("setup:write"))])
async def update_provider(body: ProviderConfigRequest) -> Dict[str, Any]:
    """Point one slot (LLM or embeddings) at a provider. The API key is
    write-only: it is stored in ``.env`` and never returned."""
    llm = body.slot == "llm"
    provider = normalize_provider_name(body.provider)
    updates: Dict[str, str] = {("DEFAULT_LLM_PROVIDER" if llm else "DEFAULT_EMBEDDING_PROVIDER"): provider}
    prefix = "LLM" if llm else "EMBEDDING"
    if body.base_url is not None:
        updates[f"{prefix}_BASE_URL"] = body.base_url.strip().rstrip("/")
    if body.model is not None:
        updates[f"{prefix}_MODEL"] = body.model.strip()
    if llm and body.summarizer_model is not None:
        updates["SUMMARIZER_MODEL"] = body.summarizer_model.strip()
    if not llm and body.dimension is not None:
        updates["EMBEDDING_DIMENSION"] = str(body.dimension) if body.dimension else ""
    key = body.api_key.get_secret_value().strip() if body.api_key else ""
    if key:
        updates[f"{prefix}_API_KEY"] = key
    elif body.clear_api_key:
        updates[f"{prefix}_API_KEY"] = ""
    try:
        applied = update_env_file(setup_env_path(), updates, allow_secrets=True)
    except ValueError as exc:
        # Messages name the key, never the value.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    previous_dimension = settings.EMBEDDING_DIMENSION
    _apply_to_settings(applied)
    notes = [
        "Background workers read .env when they start — restart them so indexing uses the new provider.",
    ]
    if not llm:
        notes.append(
            "A different embedding model means existing vectors no longer match: re-index after verifying."
        )
    return {
        "applied": sorted(applied.keys()),
        "api_key_written": bool(key),
        "dimension_changed": (not llm) and settings.EMBEDDING_DIMENSION != previous_dimension,
        "notes": notes,
        "current": _provider_view(),
    }


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
