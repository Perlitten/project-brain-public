"""Which workspace registry an offline CLI reads: the shared table in team state, the local file otherwise.

The offline CLIs (``brain.workspace.cli``, ``brain.lab.cli``,
``brain.freshness.cli``, ``brain.experiments.cli``, ``brain.portfolio.cli``,
``brain.control.cli``) run in a shell, not in the API process, so each one
decides for itself which registry to read:

* **Team state** (the team database is reachable and has live members): the
  shared ``workspace_registry_entries`` table the API and the worker read
  (``brain.team.registry``), after importing this directory's legacy file once
  while the table is empty. The CLI also leaves a marker file
  (``team_state.json``) in the registry directory, recording that this Brain
  directory's registry lives in PostgreSQL.
* **Solo state** (the database says solo, or Lite storage): the local registry
  file, as before. A marker left by an earlier team state is removed.
* **Database unreachable**: the explicit offline fallback. The CLI warns on
  stderr and reads the local file, *unless* the directory is known to be in
  team state (the marker exists) or ``BRAIN_TEAM_MODE`` is on. Then the file
  may be stale and reading it would silently diverge from the team, so the
  CLI refuses with exit code 2 (``OfflineRegistryError``). Delete the marker
  only when the Brain really is back to solo use.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from loguru import logger

from brain.workspace.registry_store import RegistryStore

if TYPE_CHECKING:  # the database layer is imported only when a team-state probe needs it
    from brain.team.registry import RegistrySnapshot

TEAM_MARKER = "team_state.json"
# Bounds the team-state probe: an unroutable database host must not hang an offline command.
PROBE_TIMEOUT_S = 10.0


class OfflineRegistryError(RuntimeError):
    """Team state is known or required, but the shared registry cannot be read."""


def marker_path(registry_dir: Path) -> Path:
    return Path(registry_dir) / TEAM_MARKER


def mark_team_state(registry_dir: Path) -> None:
    """Record that ``registry_dir`` belongs to a team-state Brain; best effort, never raises."""
    path = marker_path(registry_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "registry": "postgresql",
            "recorded_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "note": "This Brain is in team state: the workspace registry lives in PostgreSQL. Offline CLIs refuse "
                    "to read the local registry file while the team database is unreachable. Delete this file "
                    "only if the Brain is back to solo use.",
        }, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        logger.warning("Cannot write the team-state marker {}: {}", path, exc)


def clear_team_marker(registry_dir: Path) -> None:
    try:
        marker_path(registry_dir).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Cannot remove the team-state marker {}: {}", marker_path(registry_dir), exc)


async def cli_team_state(registry_dir: Path) -> bool:
    """``True`` in team state; ``False`` for solo state or the explicit offline fallback.

    Raises ``OfflineRegistryError`` when the database cannot be reached while
    ``BRAIN_TEAM_MODE`` is on or ``registry_dir`` carries the team-state marker."""
    from brain.team.authorization import strict_team_mode
    from brain.workers import initiator

    registry_dir = Path(registry_dir)
    try:
        state = await asyncio.wait_for(initiator.current_team_state(), PROBE_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - any database failure means "cannot tell"
        reason = type(exc).__name__
        if strict_team_mode():
            raise OfflineRegistryError(
                f"Cannot reach the team database to check team state ({reason}); "
                "refusing to use the local registry file while BRAIN_TEAM_MODE is on") from exc
        marker = marker_path(registry_dir)
        if marker.is_file():
            raise OfflineRegistryError(
                f"Cannot reach the team database to check team state ({reason}); this Brain is in team state "
                f"({marker} exists), so the workspace registry lives in PostgreSQL and the local registry file "
                f"may be stale. Start the database, or delete {marker} if this Brain is back to solo use") from exc
        print(f"WARNING: cannot reach the team database ({reason}); using the local registry file in "
              f"{registry_dir} (solo/offline fallback)", file=sys.stderr)
        return False
    if state == "solo":
        clear_team_marker(registry_dir)
        return False
    mark_team_state(registry_dir)
    return True


async def offline_registry(registry_dir: Path) -> RegistrySnapshot | RegistryStore:
    """The registry an offline CLI reads (see the module docstring); ``OfflineRegistryError`` in team state
    when the shared table cannot be read."""
    registry_dir = Path(registry_dir)
    if not await cli_team_state(registry_dir):
        return RegistryStore(registry_dir)
    from brain.database import session as db
    from brain.team import registry as shared

    try:
        await shared.ensure_legacy_imported(db.async_session_factory, registry_dir)
        async with db.async_session_factory() as session:
            return await shared.team_registry(session, registry_dir)
    except Exception as exc:  # noqa: BLE001 - in team state, never fall back to the file
        raise OfflineRegistryError(
            f"Cannot read the shared workspace registry in team state ({type(exc).__name__}); "
            "refusing to use the local registry file") from exc


def _refuse(exc: OfflineRegistryError) -> NoReturn:
    print(f"ERROR: {exc}", file=sys.stderr)
    sys.exit(2)


def load_registry(registry_dir: Path) -> RegistrySnapshot | RegistryStore:
    """Synchronous ``offline_registry`` for the CLIs: prints the refusal and exits with code 2."""
    try:
        return asyncio.run(offline_registry(Path(registry_dir)))
    except OfflineRegistryError as exc:
        _refuse(exc)


__all__ = ["OfflineRegistryError", "PROBE_TIMEOUT_S", "TEAM_MARKER", "clear_team_marker", "cli_team_state",
           "load_registry", "mark_team_state", "marker_path", "offline_registry"]
