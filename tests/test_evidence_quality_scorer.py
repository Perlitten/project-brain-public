"""Tests for EvidenceQualityScorer."""

from brain.evidence.models import AgentOrientedSummary, EvidencePackV2, EvidenceSufficiencyEnum
from benchmarks.utility_pilot.evaluators.evidence_quality_scorer import EvidenceQualityScorer
from benchmarks.utility_pilot.schemas.run_model import GoldManifest


def test_evidence_quality_scorer():
    gold = GoldManifest(
        task_id="task_01",
        task_name="Bug Localization",
        category="bug_localization",
        gold_review_status="single_reviewer",
        task_intent="Fix bug",
        required_behavior="Safe slicing",
        required_files=["brain/context/budget.py"],
        useful_files=["tests/test_context_budget.py"],
    )

    pack = EvidencePackV2(
        pack_id="p-1",
        route="targeted_context_pack",
        sufficiency=EvidenceSufficiencyEnum.SUFFICIENT,
        likely_relevant_files=["brain/context/budget.py", "brain/config/settings.py"],
        agent_summary=AgentOrientedSummary(
            route="targeted_context_pack",
            sufficiency="sufficient",
            recommended_files=["brain/context/budget.py", "brain/config/settings.py"],
        ),
    )

    metrics = EvidenceQualityScorer.score_pack(pack, gold)
    assert metrics["required_files_recall"] == 1.0
    assert metrics["useful_files_precision"] == 0.5
    assert metrics["irrelevant_file_rate"] == 0.5
