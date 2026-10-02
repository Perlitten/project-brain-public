"""Repository Registry Data Models.

Phase A1 — Durable repository records with explicit trust levels,
capability policies, and full registration metadata.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class TrustLevel(str, Enum):
    """Explicit trust level — never inferred from path alone."""
    TRUSTED_INTERNAL = "trusted_internal"
    TRUSTED_READ_ONLY = "trusted_read_only"
    UNTRUSTED_EXTERNAL = "untrusted_external"
    FIXTURE = "fixture"
    DISABLED = "disabled"


class RepositoryType(str, Enum):
    GIT = "git"
    LOCAL = "local"
    FIXTURE = "fixture"


class Capability(str, Enum):
    """Per-repository capability flags — default-deny."""
    READ_SOURCE = "read_source"
    READ_GIT_HISTORY = "read_git_history"
    BUILD_GRAPH = "build_graph"
    WRITE_BRAIN_METADATA = "write_brain_metadata"
    CREATE_WORKTREES = "create_worktrees"
    APPLY_CANDIDATE_PATCHES = "apply_candidate_patches"
    EXECUTE_VALIDATION = "execute_validation"
    EXPORT_PATCH = "export_patch"
    UPDATE_BASELINE = "update_baseline"
    MUTATE_POLICY = "mutate_policy"
    MUTATE_WAIVERS = "mutate_waivers"


class RegistryEventType(str, Enum):
    REGISTERED = "registered"
    UPDATED = "updated"
    CAPABILITY_CHANGED = "capability_changed"
    DISABLED = "disabled"
    RE_ENABLED = "re_enabled"
    ARCHIVED = "archived"
    PATH_CHANGED = "canonical_path_changed"
    TRUST_CHANGED = "trust_changed"


# Default capabilities per trust level
DEFAULT_CAPABILITIES: Dict[TrustLevel, List[Capability]] = {
    TrustLevel.TRUSTED_INTERNAL: [
        Capability.READ_SOURCE,
        Capability.READ_GIT_HISTORY,
        Capability.BUILD_GRAPH,
        Capability.WRITE_BRAIN_METADATA,
        Capability.CREATE_WORKTREES,
        Capability.APPLY_CANDIDATE_PATCHES,
        Capability.EXECUTE_VALIDATION,
        Capability.EXPORT_PATCH,
        Capability.UPDATE_BASELINE,
    ],
    TrustLevel.TRUSTED_READ_ONLY: [
        Capability.READ_SOURCE,
        Capability.READ_GIT_HISTORY,
        Capability.BUILD_GRAPH,
    ],
    TrustLevel.UNTRUSTED_EXTERNAL: [
        Capability.READ_SOURCE,
    ],
    TrustLevel.FIXTURE: [
        Capability.READ_SOURCE,
        Capability.READ_GIT_HISTORY,
        Capability.BUILD_GRAPH,
        Capability.CREATE_WORKTREES,
        Capability.APPLY_CANDIDATE_PATCHES,
        Capability.EXECUTE_VALIDATION,
    ],
    TrustLevel.DISABLED: [],
}


def _generate_repository_id(canonical_root: str) -> str:
    """Generate stable repository ID from canonical root hash — not from user-controlled display name."""
    content = canonical_root.replace("\\", "/").rstrip("/").lower()
    short = hashlib.sha256(content.encode()).hexdigest()[:12]
    return f"repo-{short}"


@dataclass
class RepositoryRecord:
    """Immutable-identity repository record with explicit trust and capabilities."""

    repository_id: str
    display_name: str
    canonical_root: str  # Resolved absolute path on this machine
    normalized_root_identity: str  # Portable normalized form (lowercase, forward-slash)
    repository_type: str  # RepositoryType value
    default_branch: str
    current_revision: str
    trust_level: str  # TrustLevel value
    organization: str
    owner: str
    codeowners_path: Optional[str] = None
    languages: List[str] = field(default_factory=list)
    architecture_policy_path: Optional[str] = None
    subsystem_config_path: Optional[str] = None
    graph_generation_status: str = "none"  # 'none', 'building', 'active', 'stale', 'failed'
    indexing_status: str = "none"  # 'none', 'indexing', 'indexed', 'stale', 'failed'
    capabilities: List[str] = field(default_factory=list)  # Capability values
    allowed_validation_profiles: List[str] = field(default_factory=list)
    created_at_utc: str = ""
    updated_at_utc: str = ""
    disabled: bool = False
    archived: bool = False
    version: int = 1  # Optimistic versioning

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> RepositoryRecord:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def portable_export(self) -> Dict[str, Any]:
        """Export without machine-specific absolute paths."""
        d = self.to_dict()
        d["canonical_root"] = "<redacted>"
        d["_export_identity"] = self.normalized_root_identity
        return d


@dataclass
class RegistryEvent:
    """Audit event for registry changes — structured for later ledger ingestion."""
    event_id: str
    event_type: str  # RegistryEventType value
    repository_id: str
    actor: str
    previous_version: int
    new_version: int
    timestamp_utc: str
    reason: str
    source: str  # 'cli', 'api', 'internal'
    changes: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def create(
        cls,
        event_type: RegistryEventType,
        repository_id: str,
        actor: str,
        prev_version: int,
        new_version: int,
        reason: str,
        source: str,
        changes: Optional[Dict[str, Any]] = None,
    ) -> RegistryEvent:
        return cls(
            event_id=f"evt-{uuid.uuid4().hex[:12]}",
            event_type=event_type.value,
            repository_id=repository_id,
            actor=actor,
            previous_version=prev_version,
            new_version=new_version,
            timestamp_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            reason=reason,
            source=source,
            changes=changes or {},
        )
