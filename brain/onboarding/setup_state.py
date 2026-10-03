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
Writes hold a process lock across read-modify-write and use a private temporary
file plus atomic rename, so API and MCP updates preserve each other's keys.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from contextlib import contextmanager
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
        state = json.loads(state_path().read_text())
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


@contextmanager
def _state_lock(path: Path):
    """Lock a stable sibling file, rather than the inode replaced on save."""
    fd = os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if sys.platform == "win32":
            import msvcrt

            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _save(mutator) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _state_lock(path):
        state = load_state()
        mutator(state)
        fd, name = tempfile.mkstemp(prefix=".setup-state-", dir=path.parent)
        tmp = Path(name)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(state, stream, indent=2, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)


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
