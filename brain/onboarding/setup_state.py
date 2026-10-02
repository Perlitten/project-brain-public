"""Recorded first-use verification state.

Three kinds of evidence live here, all server- or client-observable rather
than assumed:

- ``mcp_self_check``: outcome of the last server-side MCP self-check, with a
  fingerprint of the configuration it ran against — a config change
  invalidates it.
- ``client_activity``: MCP ``initialize`` handshakes recorded by the stdio
  server an external client actually spawned. This is the only thing that can
  prove Claude Code/Cursor connected.
- ``provider_verifications``: per-provider credential probe outcomes keyed by
  a fingerprint of the credential value (a truncated SHA-256 — never the key
  itself), so a changed key invalidates the old verdict.

The file lives in the user's home (``~/.project_brain/setup_state.json``):
the MCP stdio server may run from any working directory and under a different
process owner than the API, and reports/ volumes can be container-owned.
Writes are read-modify-write with atomic rename; two writers (API + spawned
MCP server) only ever touch disjoint keys.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

_STATE_DIR_ENV = "BRAIN_SETUP_STATE_DIR"
_MAX_CLIENT_EVENTS = 10


def state_dir() -> Path:
    override = os.environ.get(_STATE_DIR_ENV)
    if override:
        return Path(override)
    return Path.home() / ".project_brain"


def state_path() -> Path:
    return state_dir() / "setup_state.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_state() -> Dict[str, Any]:
    try:
        return json.loads(state_path().read_text())
    except (OSError, ValueError):
        return {}


def _save(mutator) -> None:
    state = load_state()
    mutator(state)
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    os.replace(tmp, path)


def record_self_check(readiness_status: str, fingerprint: str, detail: Dict[str, Any]) -> None:
    def _mut(state: Dict[str, Any]) -> None:
        state["mcp_self_check"] = {
            "status": readiness_status,
            "checked_at": _now(),
            "fingerprint": fingerprint,
            "detail": detail,
        }

    _save(_mut)


def record_client_activity(client_name: str, client_version: Optional[str] = None) -> None:
    """Called by the stdio MCP server when an external client handshakes."""

    def _mut(state: Dict[str, Any]) -> None:
        events = state.setdefault("client_activity", [])
        events.append(
            {
                "client": client_name,
                "version": client_version or "",
                "at": _now(),
            }
        )
        state["client_activity"] = events[-_MAX_CLIENT_EVENTS:]

    _save(_mut)


def record_provider_verification(
    slot: str,
    provider: str,
    key_fingerprint: Optional[str],
    status: str,
    error: Optional[str] = None,
) -> None:
    def _mut(state: Dict[str, Any]) -> None:
        verifications = state.setdefault("provider_verifications", {})
        verifications[slot] = {
            "provider": provider,
            "key_fingerprint": key_fingerprint,
            "status": status,
            "checked_at": _now(),
            "error": error,
        }

    _save(_mut)


def last_client_activity() -> Optional[Dict[str, Any]]:
    events = load_state().get("client_activity") or []
    return events[-1] if events else None
