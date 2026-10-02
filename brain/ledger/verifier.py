"""Evidence ledger verification — Phase F5.

Verification answers one question: is this chain the chain that was written?
It must detect an altered event, a missing event, a reordered event, a broken
previous hash, a duplicate sequence, and an unsupported schema version.
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence

from brain.ledger.models import (
    GENESIS_HASH,
    SUPPORTED_SCHEMA_VERSIONS,
    LedgerEvent,
)

# Issue codes. Stable strings: operators grep for these.
ALTERED_EVENT = "altered_event"
MISSING_EVENT = "missing_event"
REORDERED_EVENT = "reordered_event"
BROKEN_PREVIOUS_HASH = "broken_previous_hash"
DUPLICATE_SEQUENCE = "duplicate_sequence"
DUPLICATE_EVENT_ID = "duplicate_event_id"
UNSUPPORTED_SCHEMA = "unsupported_schema"
BAD_GENESIS = "bad_genesis"


def _issue(code: str, sequence: int, event_id: str, detail: str) -> Dict[str, Any]:
    return {"code": code, "sequence": sequence, "event_id": event_id, "detail": detail}


class LedgerVerifier:
    """Stateless verification of a chain presented in storage order."""

    @staticmethod
    def verify(events: Sequence[LedgerEvent]) -> Dict[str, Any]:
        issues: List[Dict[str, Any]] = []
        seen_sequences: Dict[int, str] = {}
        seen_ids: Dict[str, int] = {}
        expected_previous = GENESIS_HASH
        expected_sequence = 1

        for position, event in enumerate(events):
            # Schema first: a version we do not understand may hash by rules we
            # do not implement, so every later verdict about it is unreliable.
            if event.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
                issues.append(
                    _issue(
                        UNSUPPORTED_SCHEMA,
                        event.sequence,
                        event.event_id,
                        f"schema version {event.schema_version} is not supported by this build",
                    )
                )
                expected_previous = event.event_hash
                expected_sequence = event.sequence + 1
                continue

            if event.event_id in seen_ids:
                issues.append(
                    _issue(
                        DUPLICATE_EVENT_ID,
                        event.sequence,
                        event.event_id,
                        f"event id first seen at sequence {seen_ids[event.event_id]}",
                    )
                )
            else:
                seen_ids[event.event_id] = event.sequence

            if event.sequence in seen_sequences:
                issues.append(
                    _issue(
                        DUPLICATE_SEQUENCE,
                        event.sequence,
                        event.event_id,
                        f"sequence already used by event {seen_sequences[event.sequence]}",
                    )
                )
            else:
                seen_sequences[event.sequence] = event.event_id

            if event.sequence < expected_sequence:
                issues.append(
                    _issue(
                        REORDERED_EVENT,
                        event.sequence,
                        event.event_id,
                        f"sequence {event.sequence} appears at position {position} "
                        f"after sequence {expected_sequence - 1}",
                    )
                )
            elif event.sequence > expected_sequence:
                issues.append(
                    _issue(
                        MISSING_EVENT,
                        expected_sequence,
                        "",
                        f"expected sequence {expected_sequence}, found {event.sequence}: "
                        f"{event.sequence - expected_sequence} event(s) absent",
                    )
                )

            # Content integrity: the hash covers every field except itself, so
            # any edit anywhere in the record shows up here.
            recomputed = event.compute_hash()
            if recomputed != event.event_hash:
                issues.append(
                    _issue(
                        ALTERED_EVENT,
                        event.sequence,
                        event.event_id,
                        f"stored hash {event.event_hash[:16]}… does not match "
                        f"recomputed {recomputed[:16]}…",
                    )
                )

            if event.previous_hash != expected_previous:
                code = BAD_GENESIS if position == 0 else BROKEN_PREVIOUS_HASH
                issues.append(
                    _issue(
                        code,
                        event.sequence,
                        event.event_id,
                        f"previous hash {event.previous_hash[:16]}… does not link to "
                        f"{expected_previous[:16]}…",
                    )
                )

            expected_previous = event.event_hash
            expected_sequence = event.sequence + 1

        return {
            "valid": not issues,
            "events_checked": len(events),
            "issues": issues,
            "issue_count": len(issues),
            "codes": sorted({i["code"] for i in issues}),
            "head_hash": events[-1].event_hash if events else GENESIS_HASH,
            "head_sequence": events[-1].sequence if events else 0,
        }
