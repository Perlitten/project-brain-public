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


def test_context_regression_is_recomputed_from_paths():
    before, after = captures()
    for row in after["tasks"]:
        if row["type"] == "context":
            row["paths"] = []  # Saved hit/mrr fields remain favorable but cannot mask this.
    report = compare(before, after)
    assert not report["pass"]
    assert "context mrr regressed" in report["failures"]
