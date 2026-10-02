"""Remediation experiments — comparing candidate patches in isolated workspaces.

An experiment runs each candidate remediation option in its own disposable
workspace, compares the outcomes with an explicit deterministic formula, and
produces a recommendation.

A recommendation is not an approval. Project Brain may apply candidate patches
only inside disposable managed workspaces for validation. It does not apply
patches to authoritative repositories, commit changes, push branches, merge
pull requests, or deploy software.
"""

from brain.experiments.models import (
    ACTIONS_ALLOWING_EXPORT,
    ACTIONS_REQUIRING_CONCLUSION,
    TERMINAL_STATES,
    ComparisonResult,
    Experiment,
    ExperimentConclusion,
    ExperimentOption,
    ExperimentState,
    ExportNotAuthorizedError,
    ExportStaleError,
    HumanAction,
    HumanActionRecord,
    PatchExport,
)
from brain.experiments.orchestrator import (
    ExperimentComparator,
    ExperimentManager,
    ExperimentRunner,
    ExperimentStore,
)
from brain.experiments.packaging import (
    MANIFEST_CHECKSUM_NAME,
    MANIFEST_NAME,
    NON_AUTONOMY_STATEMENT,
    ExperimentPackager,
)

__all__ = [
    "ACTIONS_ALLOWING_EXPORT",
    "ACTIONS_REQUIRING_CONCLUSION",
    "MANIFEST_CHECKSUM_NAME",
    "MANIFEST_NAME",
    "NON_AUTONOMY_STATEMENT",
    "TERMINAL_STATES",
    "ComparisonResult",
    "Experiment",
    "ExperimentComparator",
    "ExperimentConclusion",
    "ExperimentManager",
    "ExperimentOption",
    "ExperimentPackager",
    "ExperimentRunner",
    "ExperimentState",
    "ExperimentStore",
    "ExportNotAuthorizedError",
    "ExportStaleError",
    "HumanAction",
    "HumanActionRecord",
    "PatchExport",
]
