"""Repository Registry Persistence Layer.

Phase A2 — Atomic JSON storage with optimistic versioning,
corruption detection, and deterministic serialization.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from brain.workspace.models import (
    RegistryEvent,
    RegistryEventType,
    RepositoryRecord,
    TrustLevel,
)


class RegistryCorruptionError(RuntimeError):
    """Raised when registry data fails integrity checks."""
    pass


class OptimisticLockError(RuntimeError):
    """Raised when a concurrent modification is detected."""
    pass


class RegistryStore:
    """Atomic JSON-backed repository registry with optimistic versioning.

    Implements:
    - Deterministic serialization
    - Atomic writes (write-temp → flush → replace)
    - Per-repository optimistic versioning
    - Corruption detection via content hash
    - Thread-safe access via lock
    - Event audit trail
    """

    SCHEMA_VERSION = 1

    def __init__(self, storage_dir: Path):
        self._storage_dir = storage_dir.resolve()
        self._registry_file = self._storage_dir / "repository_registry.json"
        self._events_file = self._storage_dir / "registry_events.jsonl"
        self._lock = threading.Lock()
        self._storage_dir.mkdir(parents=True, exist_ok=True)

    # --- Core CRUD ---

    def register(
        self,
        record: RepositoryRecord,
        actor: str = "system",
        source: str = "api",
        reason: str = "initial registration",
    ) -> RepositoryRecord:
        """Register a new repository. Raises if duplicate ID exists."""
        with self._lock:
            data = self._load()
            repos = data.get("repositories", {})

            if record.repository_id in repos:
                raise ValueError(
                    f"Repository '{record.repository_id}' is already registered"
                )

            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            record.created_at_utc = record.created_at_utc or now
            record.updated_at_utc = now
            record.version = 1

            repos[record.repository_id] = record.to_dict()
            data["repositories"] = repos
            self._save(data)

            self._append_event(
                RegistryEvent.create(
                    RegistryEventType.REGISTERED,
                    record.repository_id,
                    actor,
                    0,
                    1,
                    reason,
                    source,
                    {"display_name": record.display_name, "trust_level": record.trust_level},
                )
            )

            return record

    def get(self, repository_id: str) -> Optional[RepositoryRecord]:
        """Get a repository record by ID."""
        with self._lock:
            data = self._load()
            repos = data.get("repositories", {})
            raw = repos.get(repository_id)
            if raw:
                return RepositoryRecord.from_dict(raw)
            return None

    def list_all(self) -> List[RepositoryRecord]:
        """List all registered repositories."""
        with self._lock:
            data = self._load()
            repos = data.get("repositories", {})
            return [RepositoryRecord.from_dict(r) for r in repos.values()]

    def update(
        self,
        repository_id: str,
        updates: Dict[str, Any],
        expected_version: int,
        actor: str = "system",
        source: str = "api",
        reason: str = "update",
    ) -> RepositoryRecord:
        """Update a repository record with optimistic version check."""
        with self._lock:
            data = self._load()
            repos = data.get("repositories", {})
            raw = repos.get(repository_id)

            if not raw:
                raise ValueError(f"Repository '{repository_id}' not found")

            record = RepositoryRecord.from_dict(raw)

            if record.version != expected_version:
                raise OptimisticLockError(
                    f"Version conflict for '{repository_id}': "
                    f"expected {expected_version}, current {record.version}"
                )

            changes = {}
            for key, value in updates.items():
                if key in ("repository_id", "version", "created_at_utc"):
                    continue  # Immutable fields
                if hasattr(record, key):
                    old_val = getattr(record, key)
                    if old_val != value:
                        changes[key] = {"old": old_val, "new": value}
                        setattr(record, key, value)

            record.version += 1
            record.updated_at_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

            repos[repository_id] = record.to_dict()
            data["repositories"] = repos
            self._save(data)

            event_type = RegistryEventType.UPDATED
            if "trust_level" in changes:
                event_type = RegistryEventType.TRUST_CHANGED
            elif "capabilities" in changes:
                event_type = RegistryEventType.CAPABILITY_CHANGED
            elif "disabled" in changes:
                new_disabled = changes["disabled"]["new"]
                event_type = (
                    RegistryEventType.DISABLED if new_disabled
                    else RegistryEventType.RE_ENABLED
                )

            self._append_event(
                RegistryEvent.create(
                    event_type,
                    repository_id,
                    actor,
                    expected_version,
                    record.version,
                    reason,
                    source,
                    changes,
                )
            )

            return record

    def disable(
        self,
        repository_id: str,
        actor: str = "system",
        source: str = "api",
        reason: str = "disabled",
    ) -> RepositoryRecord:
        """Disable a repository."""
        record = self.get(repository_id)
        if not record:
            raise ValueError(f"Repository '{repository_id}' not found")
        return self.update(
            repository_id,
            {"disabled": True, "trust_level": TrustLevel.DISABLED.value},
            record.version,
            actor,
            source,
            reason,
        )

    def remove(self, repository_id: str) -> bool:
        """Remove a repository from the registry."""
        with self._lock:
            data = self._load()
            repos = data.get("repositories", {})
            if repository_id in repos:
                del repos[repository_id]
                data["repositories"] = repos
                self._save(data)
                return True
            return False

    # --- Events ---

    def list_events(
        self,
        repository_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[RegistryEvent]:
        """List registry audit events, optionally filtered by repository."""
        events: List[RegistryEvent] = []
        if not self._events_file.exists():
            return events

        try:
            with open(self._events_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                        evt = RegistryEvent(**{k: v for k, v in d.items() if k in RegistryEvent.__dataclass_fields__})
                        if repository_id and evt.repository_id != repository_id:
                            continue
                        events.append(evt)
                    except (json.JSONDecodeError, TypeError):
                        continue
        except OSError:
            pass

        return events[-limit:]

    def _append_event(self, event: RegistryEvent) -> None:
        """Append an audit event to the events log."""
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        with open(self._events_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(event.to_dict(), sort_keys=True) + "\n")

    # --- Persistence ---

    def _load(self) -> Dict[str, Any]:
        """Load registry data with corruption detection."""
        if not self._registry_file.exists():
            return {"schema_version": self.SCHEMA_VERSION, "repositories": {}, "content_hash": ""}

        try:
            raw = self._registry_file.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (json.JSONDecodeError, OSError) as exc:
            raise RegistryCorruptionError(f"Registry file is corrupted: {exc}") from exc

        # Verify content hash
        stored_hash = data.get("content_hash", "")
        if stored_hash:
            verify_data = {k: v for k, v in data.items() if k != "content_hash"}
            computed = hashlib.sha256(
                json.dumps(verify_data, sort_keys=True).encode()
            ).hexdigest()[:16]
            if computed != stored_hash:
                raise RegistryCorruptionError(
                    f"Registry content hash mismatch: expected {stored_hash}, computed {computed}"
                )

        return data

    def _save(self, data: Dict[str, Any]) -> None:
        """Atomic save with deterministic serialization and content hash."""
        # Compute content hash over everything except the hash itself
        hash_data = {k: v for k, v in data.items() if k != "content_hash"}
        data["content_hash"] = hashlib.sha256(
            json.dumps(hash_data, sort_keys=True).encode()
        ).hexdigest()[:16]
        data["schema_version"] = self.SCHEMA_VERSION

        self._storage_dir.mkdir(parents=True, exist_ok=True)
        temp_file = self._registry_file.with_suffix(".tmp")

        content = json.dumps(data, sort_keys=True, indent=2)
        with open(temp_file, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())

        temp_file.replace(self._registry_file)

    def verify_integrity(self) -> tuple[bool, str]:
        """Verify registry file integrity."""
        try:
            self._load()
            return True, "Registry integrity verified"
        except RegistryCorruptionError as exc:
            return False, str(exc)
        except Exception as exc:
            return False, f"Unexpected error: {exc}"
