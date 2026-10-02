"""Engineering evidence ledger — Workstream F.

An append-only, tamper-evident record of what Project Brain and its operators
did to a repository: registrations, capability changes, graph and portfolio
generations, findings, remediation plans, disposable workspaces, validation
runs, experiments, exports and human verification.

This is not a blockchain. It is a hash chain with deterministic canonical
serialization, whose only claim is that silent edits to engineering history
become detectable.

Project Brain may apply candidate patches only inside disposable managed
workspaces for validation. It does not apply patches to authoritative
repositories, commit changes, push branches, merge pull requests, or deploy
software.
"""

from brain.ledger.ledger import DEFAULT_DB_NAME, EvidenceLedger
from brain.ledger.models import (
    GENESIS_HASH,
    SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    ActorType,
    EventType,
    LedgerEvent,
    LedgerIntegrityError,
    new_event_id,
    redact_text,
    sanitize_metadata,
)
from brain.ledger.store import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    LedgerStore,
    build_event,
)
from brain.ledger.verifier import (
    ALTERED_EVENT,
    BAD_GENESIS,
    BROKEN_PREVIOUS_HASH,
    DUPLICATE_EVENT_ID,
    DUPLICATE_SEQUENCE,
    MISSING_EVENT,
    REORDERED_EVENT,
    UNSUPPORTED_SCHEMA,
    LedgerVerifier,
)

__all__ = [
    "ALTERED_EVENT",
    "BAD_GENESIS",
    "BROKEN_PREVIOUS_HASH",
    "DEFAULT_DB_NAME",
    "DEFAULT_PAGE_SIZE",
    "DUPLICATE_EVENT_ID",
    "DUPLICATE_SEQUENCE",
    "GENESIS_HASH",
    "MAX_PAGE_SIZE",
    "MISSING_EVENT",
    "REORDERED_EVENT",
    "SCHEMA_VERSION",
    "SUPPORTED_SCHEMA_VERSIONS",
    "UNSUPPORTED_SCHEMA",
    "ActorType",
    "EventType",
    "EvidenceLedger",
    "LedgerEvent",
    "LedgerIntegrityError",
    "LedgerStore",
    "LedgerVerifier",
    "build_event",
    "new_event_id",
    "redact_text",
    "sanitize_metadata",
]
