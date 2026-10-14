"""The comparison gate rejects missing inputs and real ranking regressions."""
import copy
import json
from pathlib import Path

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


def test_invalid_or_swapped_phases_cannot_pass():
    before, after = captures()
    after["phase"] = "before"
    report = compare(before, after)
    assert not report["pass"]
    assert "invalid comparison phases" in report["failures"]


def test_context_regression_is_recomputed_from_paths():
    before, after = captures()
    for row in after["tasks"]:
        if row["type"] == "context":
            row["paths"] = []  # Saved hit/mrr fields remain favorable but cannot mask this.
    report = compare(before, after)
    assert not report["pass"]
    assert "context mrr regressed" in report["failures"]


def test_task_recall_regression_is_rejected_when_first_hit_is_unchanged():
    before, after = captures()
    before = copy.deepcopy(after)
    before["phase"] = "before"
    row = next(row for row in after["tasks"]
               if row.get("type") == "context" and len(row.get("expected", [])) > 1)
    before_row = next(item for item in before["tasks"] if item["id"] == row["id"])
    before_row["paths"] = list(row["expected"])
    row["paths"] = [row["expected"][0]]
    report = compare(before, after)
    assert not report["pass"]
    assert "context recall regressed" in report["failures"]


def test_golden_scores_are_recomputed_from_captured_paths():
    before, after = captures()
    before = copy.deepcopy(after)
    before["phase"] = "before"
    for row in after["goldens"]:
        row["response_files"] = []
        row["response_chunk_files"] = []
    report = compare(before, after)
    assert not report["pass"]
    assert any(failure.startswith("golden ") for failure in report["failures"])
