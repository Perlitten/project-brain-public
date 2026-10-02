"""Registry Audit Events Emitter.

Phase A7 — Structured audit events for registry changes,
designed for later ingestion by the evidence ledger.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from brain.workspace.models import RegistryEvent, RegistryEventType


class AuditEventsEmitter:
    """Emits and stores structured registry audit events.

    Events identify: actor, repository, versions, timestamp, reason, and source.
    Structured for later evidence-ledger ingestion.
    """

    def __init__(self, events_dir: Path):
        self._events_dir = events_dir.resolve()
        self._events_dir.mkdir(parents=True, exist_ok=True)
        self._events_file = self._events_dir / "audit_events.jsonl"

    def emit(
        self,
        event_type: RegistryEventType,
        repository_id: str,
        actor: str,
        previous_version: int,
        new_version: int,
        reason: str,
        source: str,
        changes: Optional[Dict[str, Any]] = None,
    ) -> RegistryEvent:
        """Create and persist an audit event."""
        event = RegistryEvent.create(
            event_type=event_type,
            repository_id=repository_id,
            actor=actor,
            prev_version=previous_version,
            new_version=new_version,
            reason=reason,
            source=source,
            changes=changes,
        )

        with open(self._events_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(event.to_dict(), sort_keys=True) + "\n")

        return event

    def list_events(
        self,
        repository_id: Optional[str] = None,
        event_type: Optional[str] = None,
        limit: int = 100,
    ) -> List[RegistryEvent]:
        """Read persisted events with optional filters."""
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
                        evt = RegistryEvent(**{
                            k: v for k, v in d.items()
                            if k in RegistryEvent.__dataclass_fields__
                        })
                        if repository_id and evt.repository_id != repository_id:
                            continue
                        if event_type and evt.event_type != event_type:
                            continue
                        events.append(evt)
                    except (json.JSONDecodeError, TypeError):
                        continue
        except OSError:
            pass

        return events[-limit:]

    def count(self, repository_id: Optional[str] = None) -> int:
        """Count total events."""
        return len(self.list_events(repository_id=repository_id, limit=10000))

    def export(self, output_path: Path) -> int:
        """Export all events to a file. Returns count."""
        events = self.list_events(limit=100000)
        with open(output_path, "w", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e.to_dict(), sort_keys=True) + "\n")
        return len(events)
