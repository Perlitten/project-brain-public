"""Append-only Execution Journal for Project Brain v0.5.2."""

from __future__ import annotations

import datetime
import json
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class JournalEntry:
    entry_id: int
    timestamp_utc: str
    event_type: str
    phase: str
    details: Dict[str, Any] = field(default_factory=dict)


class ExecutionJournal:
    """Persists append-only execution events for auditability and restart diagnosis."""

    def __init__(self, execution_id: str, storage_dir: Optional[Path] = None):
        self.execution_id = execution_id
        self.storage_dir = storage_dir or Path(tempfile.gettempdir()) / "brain_journals"
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.journal_file = self.storage_dir / f"{execution_id}.jsonl"
        self.entries: List[JournalEntry] = []
        self._next_id = 1

    def append(self, event_type: str, phase: str, details: Optional[Dict[str, Any]] = None) -> JournalEntry:
        entry = JournalEntry(
            entry_id=self._next_id,
            timestamp_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            event_type=event_type,
            phase=phase,
            details=details or {},
        )
        self._next_id += 1
        self.entries.append(entry)
        with open(self.journal_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(entry)) + "\n")
        return entry

    def read_all(self) -> List[JournalEntry]:
        if not self.journal_file.exists():
            return []
        entries = []
        with open(self.journal_file, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    data = json.loads(line)
                    entries.append(JournalEntry(**data))
        return entries
