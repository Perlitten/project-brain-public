"""Task Definitions and Prompts for Utility Benchmark Pilot.

Contains exact prompt text, target commit, and repository information for each task.
"""

from __future__ import annotations

from typing import Dict

TASKS: Dict[str, Dict[str, str]] = {
    "task_00_calibration": {
        "task_id": "task_00_calibration",
        "title": "Calibration: Add Docstrings and Type Annotations",
        "target_commit": "HEAD",
        "prompt": (
            "Please inspect `brain/graph/identity.py` and ensure all functions and classes "
            "have complete docstrings and accurate type annotations according to PEP 484. "
            "Verify your changes by running `py -3 -m pytest tests/test_graph_v3_identity.py -v`."
        ),
    },
    "task_01_bug_localization": {
        "task_id": "task_01_bug_localization",
        "title": "Task 1: Cyrillic Token Slicing Boundary Fix",
        "target_commit": "HEAD",
        "prompt": (
            "We received a report that context packing fails when slicing strings containing "
            "multi-byte Cyrillic characters near token/byte limits in `brain/context/budget.py`. "
            "Locate the bug, fix the character slicing boundary so unicode strings are sliced safely "
            "without corrupting characters or throwing decoding errors, and verify with `py -3 -m pytest tests/test_context_budget.py -v`."
        ),
    },
    "task_02_multifile_change": {
        "task_id": "task_02_multifile_change",
        "title": "Task 2: Coordinated Export Rate Limiter Feature",
        "target_commit": "HEAD",
        "prompt": (
            "Implement a coordinated rate-limiting check for export endpoints across configuration settings, "
            "core API, and alert notifications. Specifically: add `MAX_EXPORTS_PER_MINUTE` setting in `brain/config/settings.py`, "
            "enforce the rate limit in `apps/api/routers/core.py`, and log an alert via `brain/alerts/telegram_bridge.py` when exceeded. "
            "Verify with `py -3 -m pytest tests/test_harness_api.py -v`."
        ),
    },
    "task_03_arch_boundary": {
        "task_id": "task_03_arch_boundary",
        "title": "Task 3: Enforce Search Service Architecture Boundary",
        "target_commit": "HEAD",
        "prompt": (
            "Our Drift Analyzer flagged a DRIFT-001 violation where the API layer directly accesses DB search helper logic. "
            "Refactor `apps/api/routers/core.py` to route search requests through the `SearchService` facade in `brain/search/service.py` "
            "rather than querying low-level database entities directly. Verify with `py -3 -m pytest tests/test_drift_analyzer_v3.py -v`."
        ),
    },
    "task_04_cross_repo_contract": {
        "task_id": "task_04_cross_repo_contract",
        "title": "Task 4: Portfolio API Contract Synchronization",
        "target_commit": "HEAD",
        "prompt": (
            "Update the multi-repository portfolio contract manager in `brain/portfolio/models.py` and `brain/portfolio/graph_builder.py` "
            "to ensure cross-repository dependencies are validated against declared contracts and classified cleanly into ALLOWED, FORBIDDEN, or UNDOCUMENTED. "
            "Verify with `py -3 -m pytest tests/test_portfolio_graph.py -v`."
        ),
    },
    "task_05_regression_remediation": {
        "task_id": "task_05_regression_remediation",
        "title": "Task 5: Remediation Strategy Cyclic Dependency Decoupling",
        "target_commit": "HEAD",
        "prompt": (
            "RemediationStrategyRegistry and DriftAnalyzer currently have a potential circular dependency risk. "
            "Refactor `brain/insights/remediation_strategies.py` and `brain/insights/drift_analyzer.py` to decouple top-level imports "
            "using an event or lazy interface mechanism while ensuring remediation plan creation works cleanly. "
            "Verify with `py -3 -m pytest tests/test_remediation_planner.py -v`."
        ),
    },
}
