"""Evidence ledger event model — Phases F1, F2, F4.

An append-only, tamper-evident record of engineering events. This is not a
blockchain: there is no consensus, no proof of work and no distribution. It is
a hash chain whose purpose is to make silent edits to engineering history
detectable.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = 1

#: Schema versions this build knows how to verify.
SUPPORTED_SCHEMA_VERSIONS = frozenset({1})

#: The genesis link. The first event chains from a fixed, well-known value so
#: an empty ledger and a truncated one are distinguishable.
GENESIS_HASH = "0" * 64


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class EventType(str, Enum):
    REPOSITORY_REGISTERED = "repository_registered"
    REPOSITORY_UPDATED = "repository_updated"
    CAPABILITY_CHANGED = "capability_changed"
    GRAPH_GENERATION_BUILT = "graph_generation_built"
    GRAPH_GENERATION_ACTIVATED = "graph_generation_activated"
    GRAPH_BUILD_FAILED = "graph_build_failed"
    PORTFOLIO_GENERATION_ACTIVATED = "portfolio_generation_activated"
    FINDING_CREATED = "finding_created"
    REMEDIATION_PLAN_PROPOSED = "remediation_plan_proposed"
    REMEDIATION_PLAN_APPROVED = "remediation_plan_approved"
    REMEDIATION_PLAN_REJECTED = "remediation_plan_rejected"
    WORKSPACE_CREATED = "workspace_created"
    WORKSPACE_CLEANED = "workspace_cleaned"
    WORKSPACE_QUARANTINED = "workspace_quarantined"
    PATCH_APPLIED_IN_WORKSPACE = "patch_applied_in_workspace"
    VALIDATION_COMMAND_EXECUTED = "validation_command_executed"
    EXPERIMENT_CREATED = "experiment_created"
    EXPERIMENT_COMPLETED = "experiment_completed"
    RECOMMENDATION_ACKNOWLEDGED = "recommendation_acknowledged"
    PATCH_EXPORTED = "patch_exported"
    HUMAN_MARKED_PATCH_APPLIED = "human_marked_patch_applied"
    VERIFICATION_PASSED = "verification_passed"
    VERIFICATION_FAILED = "verification_failed"
    ARTIFACT_SUPERSEDED = "artifact_superseded"
    REDACTION_RECORDED = "redaction_recorded"


class ActorType(str, Enum):
    HUMAN = "human"
    SYSTEM = "system"
    AUTOMATION = "automation"
    UNKNOWN = "unknown"


# ── Phase F4: sensitive-data handling ──

#: Metadata keys whose values are never stored verbatim.
SENSITIVE_KEY_MARKERS = (
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "PASSWD",
    "API_KEY",
    "APIKEY",
    "PRIVATE_KEY",
    "CREDENTIAL",
    "AUTHORIZATION",
    "DATABASE_URL",
    "DSN",
    "COOKIE",
    "SESSION",
)

#: Whole payloads that must never be embedded: a process environment, a raw
#: header block, or an unredacted log belongs in an artifact reference.
FORBIDDEN_KEYS = ("environment", "env", "headers", "raw_log", "stdout", "stderr")

REDACTED = "<REDACTED>"

_SECRET_VALUE_PATTERN = re.compile(
    r"(api[_-]?key|password|secret|token|private[_-]?key|bearer)"
    r"([\s=:]+)([^\s\"',;]+)",
    re.IGNORECASE,
)
_URL_CREDENTIALS = re.compile(r"(?P<scheme>[a-zA-Z][\w+.-]*://)[^/\s:@]+:[^/\s@]+@")


def redact_text(text: str) -> str:
    """Strip credential-shaped substrings from free text."""
    redacted = _URL_CREDENTIALS.sub(lambda m: f"{m.group('scheme')}{REDACTED}@", text or "")
    return _SECRET_VALUE_PATTERN.sub(rf"\1\2{REDACTED}", redacted)


def _is_sensitive_key(key: str) -> bool:
    upper = str(key).upper()
    return any(marker in upper for marker in SENSITIVE_KEY_MARKERS)


def sanitize_metadata(metadata: Optional[Dict[str, Any]]) -> tuple[Dict[str, Any], List[str]]:
    """Return (safe metadata, names of fields that were redacted).

    Sensitive values are replaced by their SHA-256 digest so an operator can
    still prove which value was involved without the ledger holding it.
    """
    safe: Dict[str, Any] = {}
    redacted_fields: List[str] = []

    def _walk(value: Any, path: str) -> Any:
        if isinstance(value, dict):
            out = {}
            for key, item in value.items():
                child_path = f"{path}.{key}" if path else str(key)
                if str(key).lower() in FORBIDDEN_KEYS:
                    redacted_fields.append(child_path)
                    out[key] = REDACTED
                elif _is_sensitive_key(key):
                    redacted_fields.append(child_path)
                    out[key] = _digest(item)
                else:
                    out[key] = _walk(item, child_path)
            return out
        if isinstance(value, list):
            return [_walk(v, f"{path}[{i}]") for i, v in enumerate(value)]
        if isinstance(value, str):
            cleaned = redact_text(value)
            if cleaned != value:
                redacted_fields.append(path)
            return cleaned
        return value

    safe = _walk(dict(metadata or {}), "")
    return safe, sorted(set(redacted_fields))


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(str(value).encode("utf-8")).hexdigest()


@dataclass
class LedgerEvent:
    """One immutable entry in the evidence chain — Phase F2."""

    event_id: str
    event_type: str
    sequence: int = 0
    schema_version: int = SCHEMA_VERSION
    timestamp_utc: str = field(default_factory=utc_now)
    actor_identity: str = "unknown"
    actor_type: str = ActorType.UNKNOWN.value
    repository_id: str = ""
    portfolio_id: str = ""
    entity_type: str = ""
    entity_id: str = ""
    action: str = ""
    previous_state: str = ""
    new_state: str = ""
    reason: str = ""
    source_revision: str = ""
    artifact_references: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    redacted_fields: List[str] = field(default_factory=list)
    previous_hash: str = GENESIS_HASH
    event_hash: str = ""

    #: Fields excluded from the canonical form. `event_hash` cannot cover
    #: itself; everything else, including `previous_hash` and `sequence`, is
    #: part of the identity so reordering or relinking is detectable.
    HASH_EXCLUDED = ("event_hash",)

    def canonical_payload(self) -> str:
        """Deterministic serialization: sorted keys, no insignificant space."""
        payload = {k: v for k, v in asdict(self).items() if k not in self.HASH_EXCLUDED}
        return json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
        )

    def compute_hash(self) -> str:
        return hashlib.sha256(self.canonical_payload().encode("utf-8")).hexdigest()

    def seal(self) -> LedgerEvent:
        self.event_hash = self.compute_hash()
        return self

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> LedgerEvent:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def new_event_id() -> str:
    return f"evt-{uuid.uuid4().hex[:16]}"


class LedgerIntegrityError(RuntimeError):
    """Raised when an append would break append-only semantics."""
