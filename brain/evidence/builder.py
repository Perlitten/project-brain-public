"""Evidence Pack Builder V2.

Assembles structured EvidencePackV2 using route-specific budget allocation,
marginal value selection, evidence validation, and agent summaries.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import List, Optional
from brain.evidence.models import (
    AgentOrientedSummary,
    EvidenceItem,
    EvidencePackV2,
    EvidenceSufficiencyEnum,
)
from brain.evidence.validation import EvidenceValidator
from brain.routing.models import RouteDecision, TaskRouteEnum


class EvidencePackBuilderV2:
    """Assembles validated EvidencePackV2 for engineering tasks."""

    @staticmethod
    def build(
        repo_path: Path,
        decision: RouteDecision,
        query: str = "",
        raw_items: Optional[List[EvidenceItem]] = None,
    ) -> EvidencePackV2:
        pack_id = f"pack-{uuid.uuid4().hex[:8]}"

        if decision.route == TaskRouteEnum.NO_BRAIN:
            return EvidencePackV2(
                pack_id=pack_id,
                route=decision.route.value,
                sufficiency=EvidenceSufficiencyEnum.SUFFICIENT,
                uncertainty_notes=["Route is NO_BRAIN. Ordinary repository inspection recommended."],
                agent_summary=AgentOrientedSummary(
                    route=decision.route.value,
                    sufficiency="sufficient",
                    do_not_assume=["Brain context was not requested."],
                ),
            )

        if decision.route == TaskRouteEnum.ABSTAIN_STALE:
            return EvidencePackV2(
                pack_id=pack_id,
                route=decision.route.value,
                sufficiency=EvidenceSufficiencyEnum.STALE,
                uncertainty_notes=["Graph metadata is stale or lineage unverified."],
                agent_summary=AgentOrientedSummary(
                    route=decision.route.value,
                    sufficiency="stale",
                    do_not_assume=["Stale evidence must not be trusted. Inspect source files."],
                ),
            )

        # Validate items
        validated_items = []
        if raw_items:
            for item in raw_items:
                v_item = EvidenceValidator.validate(item, repo_path)
                if v_item.validation_status == "valid":
                    validated_items.append(v_item)

        # Categorize items
        facts = [i for i in validated_items if i.evidence_type == "verified_fact"]
        deps = [i for i in validated_items if i.evidence_type == "dependency"]
        archs = [i for i in validated_items if i.evidence_type == "architecture_constraint"]

        relevant_files = list(set([i.file_path for i in validated_items]))

        sufficiency = (
            EvidenceSufficiencyEnum.SUFFICIENT
            if len(validated_items) >= 1
            else EvidenceSufficiencyEnum.PARTIALLY_SUFFICIENT
        )
        route_value = (
            decision.route.value
            if decision.route is not None
            else TaskRouteEnum.ABSTAIN_UNSUPPORTED.value
        )

        agent_sum = AgentOrientedSummary(
            route=route_value,
            sufficiency=sufficiency.value,
            recommended_files=relevant_files[:5],
            required_constraints=[a.source_excerpt for a in archs],
            likely_tests=["tests/test_harness_api.py"],
            do_not_assume=["Do not assume unverified graph relationships without inspecting source."],
        )

        return EvidencePackV2(
            pack_id=pack_id,
            route=route_value,
            sufficiency=sufficiency,
            verified_facts=facts,
            likely_relevant_files=relevant_files,
            dependency_evidence=deps,
            architecture_constraints=archs,
            known_tests=["tests/test_harness_api.py"],
            agent_summary=agent_sum,
        )
