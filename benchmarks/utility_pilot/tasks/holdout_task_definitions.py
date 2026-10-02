"""8-Task Unseen Holdout Task Portfolio for Project Brain Utility Benchmark.

Frozen holdout task definitions covering 100+ file codebases, multi-repo contracts,
architecture boundaries, and stale evidence abstention.
"""

HOLDOUT_TASKS = {
    "task_h1_localized_logging": {
        "task_id": "task_h1_localized_logging",
        "title": "Task H1: Localized Log Formatting Bug",
        "category": "trivial_localized_edit",
        "prompt": "Fix formatting of log messages in `brain/utils/logging.py` to strip trailing newlines.",
        "expected_route": "no_brain",
    },
    "task_h2_nonobvious_cache_eviction": {
        "task_id": "task_h2_nonobvious_cache_eviction",
        "title": "Task H2: Non-Obvious Memory Cache Eviction Bug",
        "category": "bug_localization",
        "prompt": "Locate non-obvious stale cache key retention during memory graph pruning across `brain/memory/` and `brain/graph/`.",
        "expected_route": "targeted_context_pack",
    },
    "task_h3_multifile_telemetry": {
        "task_id": "task_h3_multifile_telemetry",
        "title": "Task H3: Coordinated Multi-File Telemetry Export",
        "category": "multi_file_implementation",
        "prompt": "Implement coordinated telemetry export across `apps/api/routers/`, `brain/config/`, and `brain/alerts/`.",
        "expected_route": "targeted_context_pack",
    },
    "task_h4_arch_facade_guard": {
        "task_id": "task_h4_arch_facade_guard",
        "title": "Task H4: Architecture Facade Boundary Enforcement",
        "category": "architecture_boundary_change",
        "prompt": "Enforce strict architecture boundary between API handlers and DB layer using approved facade pattern in `apps/api/`.",
        "expected_route": "architecture_context",
    },
    "task_h5_cross_repo_schema_contract": {
        "task_id": "task_h5_cross_repo_schema_contract",
        "title": "Task H5: Cross-Repository Portfolio Schema Contract",
        "category": "cross_repository_contract_change",
        "prompt": "Synchronize portfolio schema contract definitions across API gateway and downstream workers.",
        "expected_route": "cross_repo_impact",
    },
    "task_h6_deep_impact_chain": {
        "task_id": "task_h6_deep_impact_chain",
        "title": "Task H6: Deep Transitive Impact Analysis",
        "category": "impact_analysis",
        "prompt": "Analyze and decouple transitive dependency impact chain between vector indexers and event bus.",
        "expected_route": "impact_analysis",
    },
    "task_h7_remediation_choice": {
        "task_id": "task_h7_remediation_choice",
        "title": "Task H7: Structural Remediation Choice",
        "category": "remediation_choice",
        "prompt": "Choose low-structural-risk remediation strategy for recursive cycle in AST parser.",
        "expected_route": "targeted_context_pack",
    },
    "task_h8_stale_evidence_abstention": {
        "task_id": "task_h8_stale_evidence_abstention",
        "title": "Task H8: Stale Evidence Safe Abstention",
        "category": "bug_localization",
        "prompt": "Handle invalid/stale evidence gracefully by abstaining and verifying via source file reading in `brain/context/`.",
        "expected_route": "targeted_context_pack",
    },
}
