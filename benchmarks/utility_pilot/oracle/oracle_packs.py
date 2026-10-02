"""Manually Curated Oracle Evidence Packs for Development Set.

Ideal engineering maps containing exact file references, symbols, dependencies,
and architecture constraints, with ZERO implementation code or patch text.
"""

from __future__ import annotations

from typing import Dict
from brain.evidence.models import AgentOrientedSummary, EvidenceItem, EvidencePackV2, EvidenceSufficiencyEnum


ORACLE_PACKS: Dict[str, EvidencePackV2] = {
    "task_01_bug_localization": EvidencePackV2(
        pack_id="oracle-pack-task-01",
        route="targeted_context_pack",
        sufficiency=EvidenceSufficiencyEnum.SUFFICIENT,
        verified_facts=[
            EvidenceItem(
                evidence_id="oracle-ev-101",
                repository="project-brain",
                source_revision="1bf3e0d",
                file_path="brain/context/budget.py",
                symbol="truncate_utf8",
                line_range="20-45",
                evidence_type="verified_fact",
                channel_score=1.0,
                final_score=1.0,
                freshness="current",
                confidence=1.0,
                why_included="Exact target module for utf8 character boundary slicing",
                source_excerpt="Context packing budget management module for UTF-8 string slicing.",
                provenance="oracle_curated",
            )
        ],
        likely_relevant_files=["brain/context/budget.py"],
        known_tests=["tests/test_context_budget.py"],
        agent_summary=AgentOrientedSummary(
            route="targeted_context_pack",
            sufficiency="sufficient",
            recommended_files=["brain/context/budget.py"],
            required_constraints=["Ensure multi-byte UTF-8 character boundaries (e.g. Cyrillic) are preserved without decoding errors."],
            likely_tests=["tests/test_context_budget.py"],
            do_not_assume=["Do not assume byte length equals string length in multi-byte unicode."],
        ),
    ),

    "task_02_multifile_change": EvidencePackV2(
        pack_id="oracle-pack-task-02",
        route="targeted_context_pack",
        sufficiency=EvidenceSufficiencyEnum.SUFFICIENT,
        verified_facts=[
            EvidenceItem(
                evidence_id="oracle-ev-201",
                repository="project-brain",
                source_revision="1bf3e0d",
                file_path="brain/config/settings.py",
                symbol="Settings",
                line_range="15-40",
                evidence_type="verified_fact",
                channel_score=1.0,
                final_score=1.0,
                freshness="current",
                confidence=1.0,
                why_included="Export rate limit setting definition target",
                source_excerpt="class Settings(BaseSettings):\n    # Core configuration settings for API rate limits",
                provenance="oracle_curated",
            ),
            EvidenceItem(
                evidence_id="oracle-ev-202",
                repository="project-brain",
                source_revision="1bf3e0d",
                file_path="apps/api/routers/core.py",
                symbol="export_endpoint",
                line_range="50-80",
                evidence_type="dependency",
                channel_score=1.0,
                final_score=1.0,
                freshness="current",
                confidence=1.0,
                why_included="API endpoint enforcing export rate limiting",
                source_excerpt="Router endpoints for core API and data exports.",
                provenance="oracle_curated",
            ),
            EvidenceItem(
                evidence_id="oracle-ev-203",
                repository="project-brain",
                source_revision="1bf3e0d",
                file_path="brain/alerts/telegram_bridge.py",
                symbol="send_rate_limit_alert",
                line_range="10-35",
                evidence_type="architecture_constraint",
                channel_score=1.0,
                final_score=1.0,
                freshness="current",
                confidence=1.0,
                why_included="Required alert bridge on rate limit exceeded",
                source_excerpt="Alert bridge notification handlers for rate limit enforcement.",
                provenance="oracle_curated",
            ),
        ],
        likely_relevant_files=["brain/config/settings.py", "apps/api/routers/core.py", "brain/alerts/telegram_bridge.py"],
        known_tests=["tests/test_harness_api.py"],
        agent_summary=AgentOrientedSummary(
            route="targeted_context_pack",
            sufficiency="sufficient",
            recommended_files=["brain/config/settings.py", "apps/api/routers/core.py", "brain/alerts/telegram_bridge.py"],
            required_constraints=["Settings, API router, and alert bridge must be updated in sync."],
            likely_tests=["tests/test_harness_api.py"],
            do_not_assume=["Do not omit alert notifications when export rate limits are exceeded."],
        ),
    ),

    "task_03_arch_boundary": EvidencePackV2(
        pack_id="oracle-pack-task-03",
        route="architecture_context",
        sufficiency=EvidenceSufficiencyEnum.SUFFICIENT,
        verified_facts=[
            EvidenceItem(
                evidence_id="oracle-ev-301",
                repository="project-brain",
                source_revision="1bf3e0d",
                file_path="brain/graph/identity.py",
                symbol="GraphIdentityFacade",
                line_range="10-50",
                evidence_type="architecture_constraint",
                channel_score=1.0,
                final_score=1.0,
                freshness="current",
                confidence=1.0,
                why_included="Approved architecture facade boundary",
                source_excerpt="Architecture Rule: Direct database queries from API layer must pass through GraphIdentityFacade.",
                provenance="oracle_curated",
            )
        ],
        likely_relevant_files=["brain/graph/identity.py"],
        known_tests=["tests/test_graph_v3_identity.py"],
        agent_summary=AgentOrientedSummary(
            route="architecture_context",
            sufficiency="sufficient",
            recommended_files=["brain/graph/identity.py"],
            required_constraints=["Direct DB imports in API routers violate architectural coupling rules. Use GraphIdentityFacade."],
            likely_tests=["tests/test_graph_v3_identity.py"],
            do_not_assume=["Do not bypass facade layers for quick direct queries."],
        ),
    ),

    "task_04_cross_repo_contract": EvidencePackV2(
        pack_id="oracle-pack-task-04",
        route="cross_repo_impact",
        sufficiency=EvidenceSufficiencyEnum.SUFFICIENT,
        verified_facts=[
            EvidenceItem(
                evidence_id="oracle-ev-401",
                repository="project-brain",
                source_revision="1bf3e0d",
                file_path="brain/portfolio/contract.py",
                symbol="PortfolioContract",
                line_range="15-60",
                evidence_type="verified_fact",
                channel_score=1.0,
                final_score=1.0,
                freshness="current",
                confidence=1.0,
                why_included="Portfolio API schema definition target",
                source_excerpt="Cross-repository schema definitions for portfolio tracking.",
                provenance="oracle_curated",
            )
        ],
        likely_relevant_files=["brain/portfolio/contract.py"],
        known_tests=["tests/test_portfolio_graph.py"],
        agent_summary=AgentOrientedSummary(
            route="cross_repo_impact",
            sufficiency="sufficient",
            recommended_files=["brain/portfolio/contract.py"],
            required_constraints=["Portfolio schema fields must maintain backward compatibility."],
            likely_tests=["tests/test_portfolio_graph.py"],
            do_not_assume=["Do not remove existing response fields without deprecation flags."],
        ),
    ),

    "task_05_regression_remediation": EvidencePackV2(
        pack_id="oracle-pack-task-05",
        route="targeted_context_pack",
        sufficiency=EvidenceSufficiencyEnum.SUFFICIENT,
        verified_facts=[
            EvidenceItem(
                evidence_id="oracle-ev-501",
                repository="project-brain",
                source_revision="1bf3e0d",
                file_path="brain/insights/proactive.py",
                symbol="ProactiveInsightEngine",
                line_range="20-70",
                evidence_type="verified_fact",
                channel_score=1.0,
                final_score=1.0,
                freshness="current",
                confidence=1.0,
                why_included="Regression remediation state machine target",
                source_excerpt="State machine transitions for proactive code insights.",
                provenance="oracle_curated",
            )
        ],
        likely_relevant_files=["brain/insights/proactive.py"],
        known_tests=["tests/test_proactive_insights.py"],
        agent_summary=AgentOrientedSummary(
            route="targeted_context_pack",
            sufficiency="sufficient",
            recommended_files=["brain/insights/proactive.py"],
            required_constraints=["Proactive insight state transitions must maintain L1 autonomy state tracking."],
            likely_tests=["tests/test_proactive_insights.py"],
            do_not_assume=["Do not alter state transition rules without verifying state machine persistence."],
        ),
    ),
}
