"""Engineering Change Laboratory — disposable managed validation workspaces.

Project Brain may apply candidate patches only inside disposable managed
workspaces for validation. It does not apply patches to authoritative
repositories, commit changes, push branches, merge pull requests, or deploy
software.
"""

from brain.lab.engine import (
    LabSecurityError,
    PatchApplier,
    PatchValidator,
    PostPatchAnalyzer,
    ValidationRunner,
    WorkspaceGuard,
    WorkspaceManager,
)
from brain.lab.laboratory import BUILTIN_PROFILES, ChangeLaboratory, ProfileNotFoundError
from brain.lab.models import (
    NETWORK_ISOLATION_STATUS,
    WORKSPACE_MARKER,
    CleanupReport,
    CommandDef,
    LabCapability,
    LabDeniedCapability,
    PatchApplyResult,
    PatchRecord,
    PatchSource,
    PostPatchReport,
    ValidationProfile,
    ValidationResult,
    ValidationRunReport,
    WorkspaceRecord,
    WorkspaceState,
)

__all__ = [
    "BUILTIN_PROFILES",
    "NETWORK_ISOLATION_STATUS",
    "WORKSPACE_MARKER",
    "ChangeLaboratory",
    "CleanupReport",
    "CommandDef",
    "LabCapability",
    "LabDeniedCapability",
    "LabSecurityError",
    "PatchApplier",
    "PatchApplyResult",
    "PatchRecord",
    "PatchSource",
    "PatchValidator",
    "PostPatchAnalyzer",
    "PostPatchReport",
    "ProfileNotFoundError",
    "ValidationProfile",
    "ValidationResult",
    "ValidationRunReport",
    "ValidationRunner",
    "WorkspaceGuard",
    "WorkspaceManager",
    "WorkspaceRecord",
    "WorkspaceState",
]
