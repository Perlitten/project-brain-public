"""Tests for Evidence Pack V2."""

from pathlib import Path
from brain.evidence.builder import EvidencePackBuilderV2
from brain.evidence.models import EvidenceItem, EvidenceSufficiencyEnum
from brain.evidence.validation import EvidenceValidator
from brain.routing.models import RouteDecision, TaskRouteEnum


def test_evidence_item_to_dict():
    item = EvidenceItem(
        evidence_id="ev-1",
        repository="project-brain",
        source_revision="1bf3e0d",
        file_path="brain/context/budget.py",
        symbol="truncate_utf8",
        line_range="20-35",
        evidence_type="verified_fact",
        channel_score=0.9,
        final_score=0.9,
        freshness="current",
        confidence=1.0,
        why_included="exact symbol match",
        source_excerpt="def truncate_utf8...",
    )
    d = item.to_dict()
    assert d["evidence_id"] == "ev-1"
    assert d["symbol"] == "truncate_utf8"


def test_evidence_validator(tmp_path: Path):
    item = EvidenceItem(
        evidence_id="ev-1",
        repository="project-brain",
        source_revision="1bf3e0d",
        file_path="brain/context/budget.py",
        symbol="truncate_utf8",
        line_range="20-35",
        evidence_type="verified_fact",
        channel_score=0.9,
        final_score=0.9,
        freshness="current",
        confidence=1.0,
        why_included="match",
        source_excerpt="code",
    )
    v_item = EvidenceValidator.validate(item, tmp_path)
    assert v_item.validation_status == "invalid_file_missing"

    (tmp_path / "brain" / "context").mkdir(parents=True, exist_ok=True)
    (tmp_path / "brain" / "context" / "budget.py").write_text("code")

    v_item2 = EvidenceValidator.validate(item, tmp_path)
    assert v_item2.validation_status == "valid"


def test_evidence_pack_builder_no_brain(tmp_path: Path):
    decision = RouteDecision(route=TaskRouteEnum.NO_BRAIN, confidence=0.9, reasons=["trivial"])
    pack = EvidencePackBuilderV2.build(tmp_path, decision)
    assert pack.sufficiency == EvidenceSufficiencyEnum.SUFFICIENT
    assert pack.route == "no_brain"
    assert pack.agent_summary.route == "no_brain"
