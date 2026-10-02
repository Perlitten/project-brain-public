"""Impact and Dependency Discovery Evaluator for Utility Benchmark Pilot.

Evaluates precision, recall, and relevance of files read and dependencies discovered
against the gold manifest requirements.
"""

from __future__ import annotations

from pathlib import Path
from typing import List
from benchmarks.utility_pilot.schemas.run_model import EvaluatorResult, GoldManifest


class ImpactEvaluator:
    """Evaluates file read relevance and dependency discovery precision/recall."""

    @staticmethod
    def evaluate(files_read: List[str], patch_diff: str, gold: GoldManifest) -> EvaluatorResult:
        violations = []
        details = {}

        norm_read = {Path(f).as_posix().lower() for f in files_read}
        req_files = {Path(f).as_posix().lower() for f in gold.required_files}
        useful_files = {Path(f).as_posix().lower() for f in gold.useful_files}
        allowed_set = req_files | useful_files

        # File classification
        required_read = norm_read & req_files
        useful_read = norm_read & useful_files
        irrelevant_read = norm_read - allowed_set

        recall = (len(required_read) / len(req_files)) if req_files else 1.0
        precision = (len(norm_read & allowed_set) / len(norm_read)) if norm_read else 1.0

        details["files_read_count"] = len(norm_read)
        details["required_files_count"] = len(req_files)
        details["required_read_count"] = len(required_read)
        details["useful_read_count"] = len(useful_read)
        details["irrelevant_read_count"] = len(irrelevant_read)
        details["precision"] = round(precision, 2)
        details["recall"] = round(recall, 2)

        if recall < 0.5 and req_files:
            violations.append(f"Low dependency discovery recall: {round(recall*100, 1)}%")

        score = round((recall * 60.0) + (precision * 40.0), 2)
        passed = recall >= 0.75 and score >= 60.0

        return EvaluatorResult(
            evaluator_name="impact",
            passed=passed,
            score=score,
            details=details,
            violations=violations,
        )
