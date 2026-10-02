"""Append-only evidence ledger storage — Phase F3.

Storage is SQLite: a relational engine that is already available in the
standard library, gives real transactional insertion, and can enforce
append-only semantics in the engine itself rather than by convention. A
deterministic JSONL export is produced alongside it so the chain can be
verified without the database.

Ordinary update and delete are not permitted. Retention is expressed by
appending an explicit redaction or tombstone event, never by rewriting
history.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from brain.ledger.models import (
    GENESIS_HASH,
    SCHEMA_VERSION,
    ActorType,
    EventType,
    LedgerEvent,
    LedgerIntegrityError,
    new_event_id,
    sanitize_metadata,
    utc_now,
)

#: A query that returns the whole ledger is a denial-of-service waiting to
#: happen; every read path is bounded.
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 500

_COLUMNS = (
    "sequence",
    "event_id",
    "event_type",
    "schema_version",
    "timestamp_utc",
    "actor_identity",
    "actor_type",
    "repository_id",
    "portfolio_id",
    "entity_type",
    "entity_id",
    "action",
    "previous_state",
    "new_state",
    "reason",
    "source_revision",
    "previous_hash",
    "event_hash",
    "payload",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_events (
    row_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sequence        INTEGER NOT NULL UNIQUE,
    event_id        TEXT    NOT NULL UNIQUE,
    event_type      TEXT    NOT NULL,
    schema_version  INTEGER NOT NULL,
    timestamp_utc   TEXT    NOT NULL,
    actor_identity  TEXT    NOT NULL,
    actor_type      TEXT    NOT NULL,
    repository_id   TEXT    NOT NULL DEFAULT '',
    portfolio_id    TEXT    NOT NULL DEFAULT '',
    entity_type     TEXT    NOT NULL DEFAULT '',
    entity_id       TEXT    NOT NULL DEFAULT '',
    action          TEXT    NOT NULL DEFAULT '',
    previous_state  TEXT    NOT NULL DEFAULT '',
    new_state       TEXT    NOT NULL DEFAULT '',
    reason          TEXT    NOT NULL DEFAULT '',
    source_revision TEXT    NOT NULL DEFAULT '',
    previous_hash   TEXT    NOT NULL,
    event_hash      TEXT    NOT NULL UNIQUE,
    payload         TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ledger_entity
    ON ledger_events (entity_type, entity_id, sequence);
CREATE INDEX IF NOT EXISTS idx_ledger_repository
    ON ledger_events (repository_id, sequence);
CREATE INDEX IF NOT EXISTS idx_ledger_type
    ON ledger_events (event_type, sequence);

-- Append-only is enforced by the engine, not by discipline. Retention is an
-- appended redaction event; it is never an UPDATE or a DELETE.
CREATE TRIGGER IF NOT EXISTS ledger_events_no_update
BEFORE UPDATE ON ledger_events
BEGIN
    SELECT RAISE(ABORT, 'ledger is append-only: update is not permitted');
END;

CREATE TRIGGER IF NOT EXISTS ledger_events_no_delete
BEFORE DELETE ON ledger_events
BEGIN
    SELECT RAISE(ABORT, 'ledger is append-only: delete is not permitted');
END;
"""


class LedgerStore:
    """Transactional append-only storage for the evidence chain."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
        finally:
            conn.close()

    # ── connection ──

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    # ── append ──

    def append(self, event: LedgerEvent) -> LedgerEvent:
        """Seal `event` onto the tip of the chain and insert it atomically.

        The sequence number and previous hash are assigned here, under the
        write transaction, so two concurrent writers cannot both claim the
        same link.
        """
        if event.schema_version not in (SCHEMA_VERSION,):
            raise LedgerIntegrityError(
                f"Unsupported schema version for write: {event.schema_version}"
            )
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT sequence, event_hash FROM ledger_events "
                    "ORDER BY sequence DESC LIMIT 1"
                ).fetchone()
                event.sequence = (row["sequence"] + 1) if row else 1
                event.previous_hash = row["event_hash"] if row else GENESIS_HASH
                event.seal()

                payload = json.dumps(
                    event.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
                )
                try:
                    conn.execute(
                        "INSERT INTO ledger_events ("
                        + ", ".join(_COLUMNS)
                        + ") VALUES ("
                        + ", ".join("?" for _ in _COLUMNS)
                        + ")",
                        (
                            event.sequence,
                            event.event_id,
                            event.event_type,
                            event.schema_version,
                            event.timestamp_utc,
                            event.actor_identity,
                            event.actor_type,
                            event.repository_id,
                            event.portfolio_id,
                            event.entity_type,
                            event.entity_id,
                            event.action,
                            event.previous_state,
                            event.new_state,
                            event.reason,
                            event.source_revision,
                            event.previous_hash,
                            event.event_hash,
                            payload,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    conn.execute("ROLLBACK")
                    raise LedgerIntegrityError(f"Rejected ledger append: {exc}") from exc
                conn.execute("COMMIT")
            finally:
                conn.close()
        return event

    # ── reads ──

    def get(self, event_id: str) -> Optional[LedgerEvent]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT payload FROM ledger_events WHERE event_id = ?", (event_id,)
            ).fetchone()
        finally:
            conn.close()
        return LedgerEvent.from_dict(json.loads(row["payload"])) if row else None

    def count(self) -> int:
        conn = self._connect()
        try:
            return int(conn.execute("SELECT COUNT(*) AS n FROM ledger_events").fetchone()["n"])
        finally:
            conn.close()

    def head(self) -> Optional[LedgerEvent]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT payload FROM ledger_events ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        return LedgerEvent.from_dict(json.loads(row["payload"])) if row else None

    def query(
        self,
        event_type: str = "",
        repository_id: str = "",
        entity_type: str = "",
        entity_id: str = "",
        actor_identity: str = "",
        since_sequence: int = 0,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """Bounded, paginated query over the chain — Phase F3/F6."""
        page_size = max(1, min(int(limit or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE))
        offset = max(0, int(offset or 0))

        where: List[str] = []
        params: List[Any] = []
        for column, value in (
            ("event_type", event_type),
            ("repository_id", repository_id),
            ("entity_type", entity_type),
            ("entity_id", entity_id),
            ("actor_identity", actor_identity),
        ):
            if value:
                where.append(f"{column} = ?")
                params.append(value)
        if since_sequence:
            where.append("sequence > ?")
            params.append(int(since_sequence))
        clause = (" WHERE " + " AND ".join(where)) if where else ""

        conn = self._connect()
        try:
            total = int(
                conn.execute(
                    f"SELECT COUNT(*) AS n FROM ledger_events{clause}", params
                ).fetchone()["n"]
            )
            rows = conn.execute(
                f"SELECT payload FROM ledger_events{clause} "
                "ORDER BY sequence ASC LIMIT ? OFFSET ?",
                (*params, page_size, offset),
            ).fetchall()
        finally:
            conn.close()

        events = [LedgerEvent.from_dict(json.loads(r["payload"])) for r in rows]
        return {
            "events": [e.to_dict() for e in events],
            "total": total,
            "limit": page_size,
            "offset": offset,
            "returned": len(events),
            "has_more": offset + len(events) < total,
        }

    def entity_history(
        self, entity_type: str, entity_id: str, limit: int = MAX_PAGE_SIZE, offset: int = 0
    ) -> Dict[str, Any]:
        return self.query(
            entity_type=entity_type, entity_id=entity_id, limit=limit, offset=offset
        )

    def iter_events(self) -> Iterable[LedgerEvent]:
        """Yield every event in insertion order.

        Insertion order, not sequence order: reading by `row_id` is what makes
        a swapped sequence number visible to the verifier.
        """
        conn = self._connect()
        try:
            for row in conn.execute("SELECT payload FROM ledger_events ORDER BY row_id ASC"):
                yield LedgerEvent.from_dict(json.loads(row["payload"]))
        finally:
            conn.close()

    def all_events(self) -> List[LedgerEvent]:
        return list(self.iter_events())

    # ── export ──

    def export_jsonl(self, output: Path) -> Dict[str, Any]:
        """Write a deterministic JSONL export — same ledger, same bytes."""
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        events = self.all_events()
        lines = [
            json.dumps(e.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            for e in events
        ]
        text = "".join(line + "\n" for line in lines)

        tmp = output.with_suffix(output.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(output)

        return {
            "output": str(output),
            "events": len(events),
            "head_hash": events[-1].event_hash if events else GENESIS_HASH,
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }

    @staticmethod
    def load_jsonl(path: Path) -> List[LedgerEvent]:
        """Read an export back in file order, preserving any tampering."""
        events: List[LedgerEvent] = []
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            events.append(LedgerEvent.from_dict(json.loads(line)))
        return events


def build_event(
    event_type: str,
    *,
    actor_identity: str,
    actor_type: str = ActorType.SYSTEM.value,
    repository_id: str = "",
    portfolio_id: str = "",
    entity_type: str = "",
    entity_id: str = "",
    action: str = "",
    previous_state: str = "",
    new_state: str = "",
    reason: str = "",
    source_revision: str = "",
    artifact_references: Optional[Sequence[str]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    timestamp_utc: str = "",
) -> LedgerEvent:
    """Construct a sanitized, unsealed event.

    Identity fields are normalized here so two spellings of the same actor or
    entity do not become two histories.
    """
    if isinstance(event_type, EventType):
        event_type = event_type.value
    safe_metadata, redacted = sanitize_metadata(metadata)
    return LedgerEvent(
        event_id=new_event_id(),
        event_type=str(event_type).strip().lower(),
        schema_version=SCHEMA_VERSION,
        timestamp_utc=timestamp_utc or utc_now(),
        actor_identity=_normalize_identity(actor_identity),
        actor_type=str(actor_type or ActorType.UNKNOWN.value).strip().lower(),
        repository_id=str(repository_id or "").strip(),
        portfolio_id=str(portfolio_id or "").strip(),
        entity_type=str(entity_type or "").strip().lower(),
        entity_id=str(entity_id or "").strip(),
        action=str(action or "").strip().lower(),
        previous_state=str(previous_state or "").strip(),
        new_state=str(new_state or "").strip(),
        reason=str(reason or "").strip(),
        source_revision=str(source_revision or "").strip(),
        artifact_references=sorted({str(a) for a in (artifact_references or []) if str(a)}),
        metadata=safe_metadata,
        redacted_fields=redacted,
    )


def _normalize_identity(identity: str) -> str:
    """Case-fold and trim an actor identity before it becomes part of a hash."""
    return str(identity or "unknown").strip().lower() or "unknown"
