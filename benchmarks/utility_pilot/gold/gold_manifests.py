"""Gold Standard Manifests for Utility Benchmark Pilot Tasks.

Stored isolated outside of agent-visible workspaces.
Each gold manifest defines:
- Required behavior & acceptance tests
- Required files to read/modify
- Prohibited changes & architectural constraints
- Gold review status (single_reviewer)
"""

from __future__ import annotations

from typing import Dict
from benchmarks.utility_pilot.schemas.run_model import GoldManifest, TaskCategory


def get_all_gold_manifests() -> Dict[str, GoldManifest]:
    """Return dictionary of task_id -> GoldManifest."""
    return {
        "task_00_calibration": GoldManifest(
            task_id="task_00_calibration",
            task_name="Harness Calibration Task",
            category=TaskCategory.CALIBRATION.value,
            gold_review_status="single_reviewer",
            task_intent="Calibrate benchmark harness execution and logging",
            required_behavior="Add docstring and type hints to brain/graph/identity.py",
            acceptable_solution_families=["docstring_and_type_hints"],
            prohibited_outcomes=["syntax_error", "failing_tests"],
            relevant_repositories=["project-brain"],
            required_files=["brain/graph/identity.py"],
            useful_files=["brain/graph/identity.py", "brain/graph/schema_v2.py"],
            required_dependencies=["brain.graph.schema_v2"],
            expected_impact_set=["brain/graph/identity.py"],
            mandatory_tests=["py -3 -m pytest tests/test_graph_v3_identity.py -v"],
            arch_constraints=[],
        ),
        "task_01_bug_localization": GoldManifest(
            task_id="task_01_bug_localization",
            task_name="Cyrillic Token Slicing Boundary Fix",
            category=TaskCategory.BUG_LOCALIZATION.value,
            gold_review_status="single_reviewer",
            task_intent="Fix byte vs character index slicing error when truncating context containing multi-byte Cyrillic text",
            required_behavior="Ensure context pack budgeting handles unicode character boundaries gracefully without throwing UnicodeDecodeError or chopping Cyrillic characters",
            acceptable_solution_families=["unicode_aware_slice", "encode_decode_ignore_error"],
            prohibited_outcomes=["raw_byte_slice", "silent_exception_swallowing", "failing_existing_tests"],
            relevant_repositories=["project-brain"],
            required_files=["brain/context/budget.py"],
            useful_files=["brain/context/budget.py", "brain/context/context_pack.py"],
            required_dependencies=["brain.context.budget", "brain.context.context_pack"],
            expected_impact_set=["brain/context/budget.py"],
            mandatory_tests=["py -3 -m pytest tests/test_context_budget.py -v"],
            arch_constraints=[],
        ),
        "task_02_multifile_change": GoldManifest(
            task_id="task_02_multifile_change",
            task_name="Coordinated Export Rate Limiter Feature",
            category=TaskCategory.MULTI_FILE_CHANGE.value,
            gold_review_status="single_reviewer",
            task_intent="Add configurable export rate limiter across config settings, core API, and rate limiter alert module",
            required_behavior="Export requests exceeding setting MAX_EXPORTS_PER_MINUTE return 429 Too Many Requests and trigger alert event",
            acceptable_solution_families=["middleware_rate_limiter", "router_level_rate_limiter"],
            prohibited_outcomes=["hardcoded_limits", "missing_config_setting", "bypassing_auth"],
            relevant_repositories=["project-brain"],
            required_files=["brain/config/settings.py", "apps/api/routers/core.py", "brain/alerts/telegram_bridge.py"],
            useful_files=["brain/config/settings.py", "apps/api/routers/core.py", "brain/alerts/telegram_bridge.py"],
            required_dependencies=["brain.config.settings", "apps.api.routers.core"],
            expected_impact_set=["brain/config/settings.py", "apps/api/routers/core.py"],
            mandatory_tests=["py -3 -m pytest tests/test_harness_api.py -v"],
            arch_constraints=[],
        ),
        "task_03_arch_boundary": GoldManifest(
            task_id="task_03_arch_boundary",
            task_name="Enforce Search Service Architecture Boundary",
            category=TaskCategory.ARCH_BOUNDARY.value,
            gold_review_status="single_reviewer",
            task_intent="Refactor search router to use SearchService facade instead of direct database table queries to comply with DRIFT-001",
            required_behavior="API router delegates search logic to SearchService facade; passes DriftAnalyzer DRIFT-001 check",
            acceptable_solution_families=["facade_delegation"],
            prohibited_outcomes=["direct_sql_in_router", "direct_orm_in_router", "disabling_drift_check"],
            relevant_repositories=["project-brain"],
            required_files=["apps/api/routers/core.py", "brain/search/service.py"],
            useful_files=["apps/api/routers/core.py", "brain/search/service.py", "brain/insights/drift_analyzer.py"],
            required_dependencies=["brain.search.service", "apps.api.routers.core"],
            expected_impact_set=["apps/api/routers/core.py"],
            mandatory_tests=["py -3 -m pytest tests/test_drift_analyzer_v3.py -v"],
            arch_constraints=["DRIFT-001: no direct ORM usage in API layer"],
        ),
        "task_04_cross_repo_contract": GoldManifest(
            task_id="task_04_cross_repo_contract",
            task_name="Portfolio API Contract Synchronization",
            category=TaskCategory.CROSS_REPO_CONTRACT.value,
            gold_review_status="single_reviewer",
            task_intent="Update cross-repository API contract definition and ensure provider and consumer portfolios pass contract validation",
            required_behavior="Dependencies between portfolio components remain compliant with ContractStatus.ALLOWED",
            acceptable_solution_families=["contract_declaration_update"],
            prohibited_outcomes=["unauthorized_forbidden_edge", "undocumented_dependency_creation"],
            relevant_repositories=["project-brain"],
            required_files=["brain/portfolio/models.py", "brain/portfolio/graph_builder.py"],
            useful_files=["brain/portfolio/models.py", "brain/portfolio/graph_builder.py"],
            required_dependencies=["brain.portfolio.models", "brain.portfolio.graph_builder"],
            expected_impact_set=["brain/portfolio/models.py"],
            mandatory_tests=["py -3 -m pytest tests/test_portfolio_graph.py -v"],
            arch_constraints=[],
        ),
        "task_05_regression_remediation": GoldManifest(
            task_id="task_05_regression_remediation",
            task_name="Remediation Strategy Cyclic Dependency Decoupling",
            category=TaskCategory.REGRESSION_REMEDIATION.value,
            gold_review_status="single_reviewer",
            task_intent="Fix cyclic dependency between RemediationStrategyRegistry and DriftAnalyzer by introducing an event/interface boundary",
            required_behavior="Decouple direct import, pass cycle detection test, and retain all remediation plan generation capabilities",
            acceptable_solution_families=["interface_decoupling", "event_bus_decoupling", "late_import_refactor"],
            prohibited_outcomes=["circular_import_at_top_level", "deleting_remediation_strategies"],
            relevant_repositories=["project-brain"],
            required_files=["brain/insights/remediation_strategies.py", "brain/insights/drift_analyzer.py"],
            useful_files=["brain/insights/remediation_strategies.py", "brain/insights/drift_analyzer.py", "brain/insights/cycle_detector.py"],
            required_dependencies=["brain.insights.remediation_strategies", "brain.insights.drift_analyzer"],
            expected_impact_set=["brain/insights/remediation_strategies.py"],
            mandatory_tests=["py -3 -m pytest tests/test_remediation_planner.py -v"],
            arch_constraints=["no circular dependencies between insights modules"],
        ),
    }
