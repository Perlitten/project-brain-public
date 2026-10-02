"""Change Laboratory Models — Phases D1, D3, D5.

Project Brain may apply candidate patches only inside disposable managed
workspaces for validation. It does not apply patches to authoritative
repositories, commit changes, push branches, merge pull requests, or deploy
software.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List

# Every managed workspace carries this marker. Nothing is ever deleted unless
# the marker proves the directory belongs to the expected workspace record.
WORKSPACE_MARKER = ".brain-workspace.json"

# Project Brain does not run validation inside an OS-level network namespace on
# the supported platforms, so network isolation is asserted by policy only and
# is reported as unverified rather than claimed as enforced. When a sandbox
# backend (brain/lab/sandbox.py) is active, reports carry ``enforced:<backend>``
# instead; this constant remains the no-backend fallback.
NETWORK_ISOLATION_STATUS = "unverified"


class WorkspaceState(str, Enum):
    REQUESTED = "requested"
    PREPARING = "preparing"
    READY = "ready"
    PATCHING = "patching"
    VALIDATING = "validating"
    PASSED = "passed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    CLEANING = "cleaning"
    CLEANED = "cleaned"
    QUARANTINED = "quarantined"


class LabCapability(str, Enum):
    CREATE_WORKTREE = "create_worktree"
    COPY_FIXTURE = "copy_fixture"
    APPLY_PATCH = "apply_patch"
    READ_FILES = "read_files"
    RUN_VALIDATION = "run_validation"
    GENERATE_DIFF = "generate_diff"
    EXPORT_PATCH = "export_patch"
    REMOVE_WORKSPACE = "remove_workspace"


class LabDeniedCapability(str, Enum):
    NETWORK_ACCESS = "network_access"
    ARBITRARY_SHELL = "arbitrary_shell"
    ARBITRARY_EXEC = "arbitrary_exec"
    WRITE_OUTSIDE = "write_outside"
    READ_HOME = "read_home"
    READ_UNRELATED = "read_unrelated"
    READ_SECRETS = "read_secrets"
    ACCESS_PROD_CREDS = "access_prod_creds"
    COMMIT = "commit"
    PUSH = "push"
    DEPLOY = "deploy"


class PatchSource(str, Enum):
    REMEDIATION_PLAN = "remediation_plan"
    UNIFIED_DIFF = "unified_diff"
    SAFE_TRANSFORM = "safe_transform"
    TEST_FIXTURE = "test_fixture"


@dataclass
class CommandDef:
    argv: List[str] = field(default_factory=list)
    required: bool = True
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ValidationProfile:
    profile_name: str = "python-standard"
    timeout_seconds: int = 900
    environment: Dict[str, str] = field(default_factory=dict)
    commands: List[CommandDef] = field(default_factory=list)
    schema_version: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> ValidationProfile:
        cmds = [
            CommandDef(**{k: v for k, v in c.items() if k in CommandDef.__dataclass_fields__})
            for c in d.get("commands", [])
        ]
        return cls(
            profile_name=d.get("profile_name", "python-standard"),
            timeout_seconds=d.get("timeout_seconds", 900),
            environment=d.get("environment", {}),
            commands=cmds,
            schema_version=d.get("schema_version", 1),
        )


@dataclass
class ValidationResult:
    command_idx: int
    argv: List[str] = field(default_factory=list)
    exit_code: int = -1
    stdout_snippet: str = ""
    stderr_snippet: str = ""
    duration_seconds: float = 0.0
    timed_out: bool = False
    required: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ValidationRunReport:
    """Aggregate outcome of running one validation profile."""

    workspace_id: str = ""
    profile_name: str = ""
    passed: bool = False
    cancelled: bool = False
    rejected_reasons: List[str] = field(default_factory=list)
    # Reported, never claimed: see NETWORK_ISOLATION_STATUS.
    network_isolation: str = NETWORK_ISOLATION_STATUS
    results: List[Dict] = field(default_factory=list)
    duration_seconds: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PatchRecord:
    patch_id: str
    source_type: str = PatchSource.TEST_FIXTURE.value
    repository_id: str = ""
    base_revision: str = ""
    expected_file_hashes: Dict[str, str] = field(default_factory=dict)
    patch_content: str = ""
    max_files: int = 50
    max_bytes: int = 500000
    allowed_paths: List[str] = field(default_factory=list)
    rejected_reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> PatchRecord:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class WorkspaceRecord:
    workspace_id: str
    repository_id: str
    base_revision: str
    candidate_patch_id: str = ""
    workspace_root: str = ""
    source_repository_root: str = ""
    creation_method: str = ""  # 'git_worktree' or 'copy'
    state: str = WorkspaceState.REQUESTED.value
    capabilities: List[str] = field(default_factory=list)
    created_at_utc: str = ""
    updated_at_utc: str = ""
    expires_at_utc: str = ""
    process_owner: str = ""
    cleanup_status: str = ""
    quarantine_reason: str = ""
    last_error: str = ""
    patch_files: List[str] = field(default_factory=list)
    before_hashes: Dict[str, str] = field(default_factory=dict)
    after_hashes: Dict[str, str] = field(default_factory=dict)
    validation_results: List[Dict] = field(default_factory=list)
    version: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> WorkspaceRecord:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class PatchApplyResult:
    """Outcome of applying one candidate patch inside one workspace."""

    workspace_id: str = ""
    patch_id: str = ""
    success: bool = False
    affected_files: List[str] = field(default_factory=list)
    before_hashes: Dict[str, str] = field(default_factory=dict)
    after_hashes: Dict[str, str] = field(default_factory=dict)
    rejected_reasons: List[str] = field(default_factory=list)
    rolled_back: bool = False
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PostPatchReport:
    """What the patch actually did to the workspace tree."""

    workspace_id: str = ""
    changed_files: List[str] = field(default_factory=list)
    added_files: List[str] = field(default_factory=list)
    deleted_files: List[str] = field(default_factory=list)
    declared_files: List[str] = field(default_factory=list)
    unexpected_files: List[str] = field(default_factory=list)
    within_declared_scope: bool = True
    analysis_method: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CleanupReport:
    """Result of disposing of a workspace, or of an orphan sweep."""

    workspace_id: str = ""
    removed: bool = False
    quarantined: bool = False
    reason: str = ""
    orphan_directories: List[str] = field(default_factory=list)
    missing_workspaces: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
