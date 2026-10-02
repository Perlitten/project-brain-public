"""Tests for EvaluatorV2."""

from pathlib import Path
from benchmarks.utility_pilot.evaluators.evaluator_v2 import EvaluatorV2
from benchmarks.utility_pilot.schemas.run_model import GoldManifest


def test_evaluator_v2_empty_patch(tmp_path: Path):
    gold = GoldManifest(
        task_id="task_01",
        task_name="Bug Localization",
        category="bug_localization",
        gold_review_status="single_reviewer",
        task_intent="Fix bug",
        required_behavior="Safe slicing",
        mandatory_tests=["py -3 -m pytest tests/test_context_budget.py -v"],
        required_files=["brain/context/budget.py"],
    )
    success, legacy_score, diag_score_v2, breakdown = EvaluatorV2.evaluate_run(tmp_path, "", [], gold)
    assert success is False
    assert legacy_score >= 0.0
    assert diag_score_v2 <= 30.0  # Evaluator V2 eliminates artificial 70+ floor for empty patches!


def test_evaluator_v2_correct_patch(tmp_path: Path):
    gold = GoldManifest(
        task_id="task_01",
        task_name="Bug Localization",
        category="bug_localization",
        gold_review_status="single_reviewer",
        task_intent="Fix bug",
        required_behavior="Safe slicing",
        mandatory_tests=["py -3 -m pytest tests/test_context_budget.py -v"],
        required_files=["brain/context/budget.py"],
    )
    patch = "diff --git a/brain/context/budget.py b/brain/context/budget.py\n+ # fix"
    files_read = ["brain/context/budget.py"]
    success, legacy_score, diag_score_v2, breakdown = EvaluatorV2.evaluate_run(tmp_path, patch, files_read, gold)
    assert diag_score_v2 >= 10.0
