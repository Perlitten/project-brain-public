"""The workspace registry in team state: one PostgreSQL table every process reads.

Repository *trust* lives in the workspace registry. The original registry is a
file (``.brain/workspace/repository_registry.json``) in each process's working
directory, so the API, the worker and the offline CLI each read their own copy.
In team state that is not good enough: the worker re-authorizes a queued job
(``brain.workers.initiator``) and must see the trust the API saw. Team state
therefore keeps the registry in ``workspace_registry_entries`` (and its audit
trail in ``workspace_registry_events``), read through the caller's own session:

* ``trust_records`` is what ``authorize_repository`` caps membership with;
* ``team_registry`` / ``team_events`` serve the registry routes and the CLI;
* ``insert_entry`` / ``disable_entry`` / ``enable_entry`` change it inside the
  caller's transaction, so a project link and an activity row commit with it.

Legacy file: while the table is empty, the file of the reading process is the
fallback (logged), and ``import_legacy_registry`` copies it into the table once
(the API on startup and on first team-state use, the CLI before each team-state
command; never the worker, whose own file is not authoritative). Once the table
has entries the file is ignored in team state. Solo state keeps using the file
and never opens this module."""
from __future__ import annotations

import time
import uuid
import weakref
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain.database.repository_utils import find_repository_id
from brain.team.activity import record_event
from brain.team.models import ProjectRepository, WorkspaceRegistryEntry, WorkspaceRegistryEvent
from brain.workspace.models import RegistryEvent, RegistryEventType, RepositoryRecord, TrustLevel
from brain.workspace.registry_store import RegistryCorruptionError, RegistryStore
from brain.workspace.trust import DEFAULT_REGISTRY_DIR, load_registry_records

REGISTRY_FILE = "repository_registry.json"
# Serializes concurrent legacy imports (API workers, CLI) on PostgreSQL.
IMPORT_LOCK_ID = 8675310
_ALL_EVENTS = 1_000_000


class RegistryConflict(Exception):
    """The registry entry is not in a state the requested change applies to."""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _legacy_dir(registry_dir: Path | None) -> Path:
    return registry_dir or DEFAULT_REGISTRY_DIR


def _entry(record: RepositoryRecord) -> WorkspaceRegistryEntry:
    return WorkspaceRegistryEntry(repository_id=record.repository_id,
                                  normalized_root_identity=record.normalized_root_identity[:1024],
                                  trust_level=record.trust_level, disabled=bool(record.disabled),
                                  version=record.version, record=record.to_dict())


def _event_row(event: RegistryEvent) -> WorkspaceRegistryEvent:
    return WorkspaceRegistryEvent(event_id=event.event_id, repository_id=event.repository_id,
                                  event_type=event.event_type, event=event.to_dict())


def _event(payload: dict[str, Any]) -> RegistryEvent:
    return RegistryEvent(**{key: value for key, value in payload.items() if key in RegistryEvent.__dataclass_fields__})


class RegistrySnapshot:
    """Read-only view of the table for code written against ``RegistryStore`` reads."""

    def __init__(self, records: list[RepositoryRecord]):
        self._records = {record.repository_id: record for record in records}

    def get(self, repository_id: str) -> RepositoryRecord | None:
        return self._records.get(repository_id)

    def list_all(self) -> list[RepositoryRecord]:
        return list(self._records.values())

    def verify_integrity(self) -> tuple[bool, str]:
        return True, f"Registry is stored in PostgreSQL ({len(self._records)} repositories)"


async def table_records(session: AsyncSession) -> list[RepositoryRecord]:
    rows = (await session.execute(
        select(WorkspaceRegistryEntry.record).order_by(WorkspaceRegistryEntry.repository_id))).scalars().all()
    return [RepositoryRecord.from_dict(row) for row in rows]


async def table_record(session: AsyncSession, repository_id: str) -> RepositoryRecord | None:
    row = (await session.execute(select(WorkspaceRegistryEntry.record).where(
        WorkspaceRegistryEntry.repository_id == repository_id))).scalar_one_or_none()
    return RepositoryRecord.from_dict(row) if row is not None else None


async def table_events(session: AsyncSession, repository_id: str | None = None,
                       limit: int = 100) -> list[RegistryEvent]:
    """The newest ``limit`` events, oldest first (the order ``RegistryStore.list_events`` returns)."""
    statement = select(WorkspaceRegistryEvent.event).order_by(WorkspaceRegistryEvent.id.desc()).limit(limit)
    if repository_id:
        statement = statement.where(WorkspaceRegistryEvent.repository_id == repository_id)
    rows = (await session.execute(statement)).scalars().all()
    return [_event(row) for row in reversed(rows)]


async def _table_empty(session: AsyncSession) -> bool:
    return (await session.execute(select(WorkspaceRegistryEntry.repository_id).limit(1))).scalar_one_or_none() is None


def _log_fallback(directory: Path, count: int) -> None:
    logger.warning("Workspace registry table is empty; reading the legacy registry file in {} ({} repositories) "
                   "until it is imported", directory, count)


async def trust_records(session: AsyncSession, registry_dir: Path | None = None) -> list[RepositoryRecord]:
    """Registry records for a team-state trust decision: the table, or the legacy file while it is empty.

    Raises ``RegistryCorruptionError`` (or ``OSError``) when the fallback file
    is unreadable, so the caller can fail closed."""
    records = await table_records(session)
    if records:
        return records
    legacy = load_registry_records(registry_dir)
    if legacy:
        _log_fallback(_legacy_dir(registry_dir), len(legacy))
    return legacy


async def team_registry(session: AsyncSession, registry_dir: Path | None = None) -> RegistrySnapshot | RegistryStore:
    """The registry to read in team state: the table, or the legacy file store while the table is empty."""
    records = await table_records(session)
    if records:
        return RegistrySnapshot(records)
    directory = _legacy_dir(registry_dir)
    store = RegistryStore(directory)
    legacy = store.list_all()
    if legacy:
        _log_fallback(directory, len(legacy))
    return store


async def team_events(session: AsyncSession, repository_id: str | None, limit: int,
                      registry_dir: Path | None = None) -> list[RegistryEvent]:
    if not await _table_empty(session):
        return await table_events(session, repository_id, limit)
    return RegistryStore(_legacy_dir(registry_dir)).list_events(repository_id=repository_id, limit=limit)


async def import_legacy_registry(session: AsyncSession, registry_dir: Path | None = None) -> int:
    """Copy the legacy registry file (records and events) into the empty table; the number imported.

    A no-op when the file is missing or empty or the table already has entries,
    so it is safe to call on every startup. Runs in the caller's transaction."""
    directory = _legacy_dir(registry_dir)
    if not (directory / REGISTRY_FILE).is_file():
        return 0
    if session.get_bind().dialect.name == "postgresql":
        await session.execute(text(f"SELECT pg_advisory_xact_lock({IMPORT_LOCK_ID})"))
    if not await _table_empty(session):
        return 0
    store = RegistryStore(directory)
    records = store.list_all()
    if not records:
        return 0
    session.add_all(_entry(record) for record in records)
    seen: set[str] = set()
    for event in store.list_events(limit=_ALL_EVENTS):
        if event.event_id in seen:
            continue
        seen.add(event.event_id)
        session.add(_event_row(event))
    await session.flush()
    logger.warning("Imported {} workspace registry entries and {} events from the legacy file {} into "
                   "workspace_registry_entries; the file is no longer read in team state",
                   len(records), len(seen), directory / REGISTRY_FILE)
    return len(records)


# Session factories whose table is known to have entries, per legacy directory: skips the check per request.
_settled: weakref.WeakKeyDictionary[async_sessionmaker, set[str]] = weakref.WeakKeyDictionary()


async def ensure_legacy_imported(factory: async_sessionmaker, registry_dir: Path | None = None) -> int:
    """Import the legacy file in its own transaction unless that already happened; never raises on a bad file.

    An unreadable file is logged and left alone: trust decisions read it as the
    fallback and fail closed (``trust_records``)."""
    directory = _legacy_dir(registry_dir)
    key = str(directory.resolve())
    if key in _settled.get(factory, set()) or not (directory / REGISTRY_FILE).is_file():
        return 0
    try:
        async with factory() as session, session.begin():
            imported = await import_legacy_registry(session, directory)
            settled = not await _table_empty(session)
    except (RegistryCorruptionError, OSError) as exc:
        logger.error("Legacy workspace registry {} cannot be imported: {}", directory, exc)
        return 0
    if settled:
        _settled.setdefault(factory, set()).add(key)
    return imported


async def insert_entry(session: AsyncSession, record: RepositoryRecord, *, actor: str, source: str,
                       reason: str = "initial registration") -> RepositoryRecord:
    """Register ``record`` in the caller's transaction; ``ValueError`` when its id is taken."""
    if await table_record(session, record.repository_id) is not None:
        raise ValueError(f"Repository '{record.repository_id}' is already registered")
    now = _now()
    record.created_at_utc = record.created_at_utc or now
    record.updated_at_utc = now
    record.version = 1
    session.add(_entry(record))
    session.add(_event_row(RegistryEvent.create(
        RegistryEventType.REGISTERED, record.repository_id, actor, 0, 1, reason, source,
        {"display_name": record.display_name, "trust_level": record.trust_level})))
    await session.flush()
    return record


async def _change(session: AsyncSession, repository_id: str, updates: dict[str, Any], event_type: RegistryEventType,
                  *, actor: str, source: str, reason: str) -> RepositoryRecord:
    row = (await session.execute(select(WorkspaceRegistryEntry).where(
        WorkspaceRegistryEntry.repository_id == repository_id).with_for_update())).scalar_one_or_none()
    if row is None:
        raise ValueError(f"Repository '{repository_id}' not found")
    record = RepositoryRecord.from_dict(row.record)
    changes = {key: {"old": getattr(record, key), "new": value}
               for key, value in updates.items() if getattr(record, key) != value}
    for key, value in updates.items():
        setattr(record, key, value)
    previous = record.version
    record.version = previous + 1
    record.updated_at_utc = _now()
    row.record = record.to_dict()
    row.trust_level = record.trust_level
    row.disabled = bool(record.disabled)
    row.version = record.version
    session.add(_event_row(RegistryEvent.create(event_type, repository_id, actor, previous, record.version, reason,
                                                source, changes)))
    await session.flush()
    return record


async def disable_entry(session: AsyncSession, repository_id: str, *, actor: str, source: str,
                        reason: str) -> RepositoryRecord:
    """Withdraw trust (the same change ``RegistryStore.disable`` makes); ``ValueError`` when unknown."""
    return await _change(session, repository_id, {"disabled": True, "trust_level": TrustLevel.DISABLED.value},
                         RegistryEventType.DISABLED, actor=actor, source=source, reason=reason)


def is_disabled(record: RepositoryRecord) -> bool:
    return bool(record.disabled) or record.trust_level == TrustLevel.DISABLED.value


def restored_trust_level(events: list[RegistryEvent], requested: str | None) -> str:
    """The trust level a re-enabled entry gets: ``requested``, else the level it had before it was disabled.

    Raises ``ValueError`` for an unknown level and ``RegistryConflict`` when
    ``requested`` is ``disabled`` or no earlier level is recorded."""
    if requested:
        level = TrustLevel(requested)
        if level == TrustLevel.DISABLED:
            raise RegistryConflict("Re-enabling needs a trust level other than 'disabled'")
        return level.value
    for event in reversed(events):
        change = (event.changes or {}).get("trust_level")
        if isinstance(change, dict) and change.get("new") == TrustLevel.DISABLED.value:
            old = change.get("old")
            if old and old != TrustLevel.DISABLED.value and old in {level.value for level in TrustLevel}:
                return str(old)
    raise RegistryConflict("The trust level the repository had before it was disabled is not recorded; "
                           "pass trust_level explicitly")


async def enable_entry(session: AsyncSession, repository_id: str, *, trust_level: str | None, actor: str,
                       source: str, reason: str) -> RepositoryRecord:
    """Re-enable a disabled entry with ``restored_trust_level``; ``ValueError`` when unknown."""
    record = await table_record(session, repository_id)
    if record is None:
        raise ValueError(f"Repository '{repository_id}' not found")
    if not is_disabled(record):
        raise RegistryConflict(f"Repository '{repository_id}' is not disabled")
    level = restored_trust_level(await table_events(session, repository_id, _ALL_EVENTS), trust_level)
    return await _change(session, repository_id, {"disabled": False, "trust_level": level},
                         RegistryEventType.RE_ENABLED, actor=actor, source=source, reason=reason)


async def audit(session: AsyncSession, kind: str, record: RepositoryRecord, summary: str, *,
                actor_principal_id: int | None = None, actor_label: str | None = None,
                project_id: uuid.UUID | None = None, details: dict | None = None) -> None:
    """One ``registry.<kind>`` row in the team activity feed (administrators only), in the caller's transaction."""
    repository_id = await find_repository_id(session, record.canonical_root)
    if project_id is None and repository_id is not None:
        project_id = (await session.execute(select(ProjectRepository.project_id).where(
            ProjectRepository.repository_id == repository_id))).scalar_one_or_none()
    await record_event(session, kind=f"registry.{kind}", summary=summary, audience="admin",
                       actor_principal_id=actor_principal_id, actor_label=actor_label, project_id=project_id,
                       repository_id=repository_id,
                       details={"registry_id": record.repository_id, "trust_level": record.trust_level,
                                **(details or {})})


__all__ = ["RegistryConflict", "RegistrySnapshot", "audit", "disable_entry", "enable_entry", "ensure_legacy_imported",
           "import_legacy_registry", "insert_entry", "is_disabled", "restored_trust_level", "table_events",
           "table_record", "table_records", "team_events", "team_registry", "trust_records"]
