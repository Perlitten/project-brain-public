"""The comparison gate rejects missing inputs and real ranking regressions."""
import copy
import json
from pathlib import Path

import pytest

from eval.product_upgrade_gate import compare


def captures():
    evidence = Path(__file__).resolve().parents[1] / "docs/release/evidence"
    return tuple(json.loads((evidence / f"product-upgrade-{phase}.json").read_text(encoding="utf-8"))
                 for phase in ("before", "after"))


def test_captured_nonregression_preserves_individual_misses():
    before, after = captures()
    assert compare(before, after)["pass"]
    assert any(row["id"] == "context-006" and not row["recall"] for row in after["tasks"])


def test_removed_task_or_changed_expected_file_cannot_pass():
    before, after = captures()
    missing = copy.deepcopy(after)
    missing["tasks"].pop()
    assert not compare(before, missing)["pass"]
    after["goldens"][0]["expected_files"] = ["invented.py"]
    assert not compare(before, after)["pass"]


def test_context_regression_is_recomputed_from_paths():
    before, after = captures()
    for row in after["tasks"]:
        if row["type"] == "context":
            row["paths"] = []  # Saved hit/mrr fields remain favorable but cannot mask this.
    report = compare(before, after)
    assert not report["pass"]
    assert "context mrr regressed" in report["failures"]


def test_required_file_recall_loss_fails_even_when_first_hit_is_unchanged():
    before, _ = captures()
    after = copy.deepcopy(before)
    after["phase"] = "after"
    old = next(row for row in before["tasks"] if row["id"] == "context-004")
    new = next(row for row in after["tasks"] if row["id"] == old["id"])
    assert len(old["expected"]) == 2
    old["paths"] = old["expected"][:]
    new["paths"] = old["expected"][:1]
    report = compare(before, after)
    assert not report["pass"]
    assert report["tasks"]["context"]["before"]["hit1"] == report["tasks"]["context"]["after"]["hit1"]
    assert "context recall regressed" in report["failures"]
    assert "context-004 required-file recall regressed" in report["failures"]


def test_individual_recall_loss_cannot_be_offset_by_other_task_gains():
    before, after = captures()
    old = next(row for row in before["tasks"] if row["id"] == "context-004")
    new = next(row for row in after["tasks"] if row["id"] == old["id"])
    old["paths"] = old["expected"][:]
    new["paths"] = old["expected"][:1]
    report = compare(before, after)
    assert report["tasks"]["context"]["after"]["recall"] > report["tasks"]["context"]["before"]["recall"]
    assert not report["pass"]
    assert "context-004 required-file recall regressed" in report["failures"]


def test_golden_regression_is_recomputed_from_paths_despite_favorable_saved_scores():
    before, after = captures()
    for row in after["goldens"]:
        row["response_files"] = []
        row["response_chunk_files"] = []
    report = compare(before, after)
    assert not report["pass"]
    assert report["goldens"]["after"]["hit_1_pct"] == 0
    assert report["goldens"]["after"]["mrr"] == 0
    assert "golden mrr regressed" in report["failures"]


def test_corrupted_saved_baseline_scores_do_not_lower_the_bar():
    before, after = captures()
    for row in before["goldens"]:
        row.update(hit_at_1=False, hit_at_3=False, hit_at_5=False, mrr=0, rank_any=0)
    report = compare(before, after)
    assert report["goldens"]["before"]["hit_1_pct"] == 45
    assert report["goldens"]["before"]["mrr"] == 0.5746


@pytest.mark.parametrize("phases", [("after", "after"), ("before", "before"), ("after", "before"), ("before", "final")])
def test_invalid_swapped_and_self_comparison_phases_fail(phases):
    before, after = captures()
    before["phase"], after["phase"] = phases
    report = compare(before, after)
    assert not report["pass"]
    assert "capture phases must be before, after in that order" in report["failures"]


@pytest.mark.asyncio
async def test_capture_rejects_unknown_phase_before_credentials_or_http(monkeypatch):
    from argparse import Namespace
    from eval.product_upgrade_capture import capture
    monkeypatch.delenv("BRAIN_API_KEY", raising=False)
    with pytest.raises(ValueError, match="capture phase"):
        await capture(Namespace(phase="final"))
