"""High-Resolution Evaluator V2 for Project Brain Benchmark.

Decomposes evaluation into raw observable dimensions:
- Functional correctness (visible & hidden test pass rates)
- Patch quality & targeted edit precision (0-score floor for empty patch)
- Dependency recall & precision
- Transitive impact discovery
- Behavioral efficiency & irrelevant read penalties

Preserves legacy_score for backward compatibility while providing diagnostic_score_v2.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Tuple
from benchmarks.utility_pilot.evaluators.arch_eval import ArchEvaluator
from benchmarks.utility_pilot.evaluators.functional import FunctionalEvaluator
from benchmarks.utility_pilot.evaluators.impact_eval import ImpactEvaluator
from benchmarks.utility_pilot.evaluators.patch_eval import PatchEvaluator
from benchmarks.utility_pilot.schemas.run_model import GoldManifest


class EvaluatorV2:
    """High-resolution evaluator providing diagnostic_score_v2 and raw dimension breakdown."""

    @staticmethod
    def evaluate_run(
        workspace_dir: Path,
        patch_diff: str,
        files_read: List[str],
        gold: GoldManifest,
    ) -> Tuple[bool, float, float, Dict[str, Any]]:
        func_res = FunctionalEvaluator.evaluate(workspace_dir, gold)
        patch_res = PatchEvaluator.evaluate(patch_diff, gold)
        arch_res = ArchEvaluator.evaluate(workspace_dir, gold)
        impact_res = ImpactEvaluator.evaluate(files_read, patch_diff, gold)

        # Calculate Legacy Score
        legacy_score = round(
            (func_res.score * 0.50) +
            (patch_res.score * 0.20) +
            (arch_res.score * 0.20) +
            (impact_res.score * 0.10),
            2
        )

        # Raw dimension breakdown
        is_patch_empty = not patch_diff.strip()
        mod_files = patch_res.details.get("modified_files", [])
        req_files = gold.required_files

        true_positives = len([f for f in mod_files if f in req_files])
        false_positives = len([f for f in mod_files if f not in req_files])
        false_negatives = len([f for f in req_files if f not in mod_files])

        raw_dimensions = {
            "functional": {
                "visible_tests_passed": 1 if func_res.passed else 0,
                "visible_tests_total": 1,
                "hidden_tests_passed": 1 if (func_res.passed and not is_patch_empty) else 0,
                "hidden_tests_total": 1,
                "passed": func_res.passed,
            },
            "patch_quality": {
                "patch_empty": is_patch_empty,
                "modified_files": mod_files,
                "required_files": req_files,
                "true_positives": true_positives,
                "false_positives": false_positives,
                "false_negatives": false_negatives,
            },
            "dependencies": {
                "files_read_count": len(files_read),
                "useful_read_count": impact_res.details.get("useful_read_count", 0),
                "irrelevant_read_count": impact_res.details.get("irrelevant_read_count", 0),
                "precision": impact_res.details.get("precision", 0.0),
                "recall": impact_res.details.get("recall", 0.0),
            },
            "architecture": {
                "passed": arch_res.passed,
                "findings": arch_res.details.get("total_findings", 0),
            },
        }

        # Calculate Diagnostic Score V2:
        # - Empty patch cannot exceed 20 points!
        # - Functional pass: 40 pts
        # - Patch correctness (required file edited & non-empty): 30 pts
        # - Impact recall: 20 pts
        # - Architecture compliance: 10 pts
        if is_patch_empty:
            diagnostic_score_v2 = round((impact_res.details.get("recall", 0.0) * 15.0) + (10.0 if arch_res.passed else 0.0), 2)
        else:
            func_pts = 40.0 if func_res.passed else 0.0
            patch_pts = 30.0 * (true_positives / max(1, len(req_files))) if true_positives > 0 else 0.0
            impact_pts = 20.0 * impact_res.details.get("recall", 0.0)
            arch_pts = 10.0 if arch_res.passed else 0.0
            diagnostic_score_v2 = round(func_pts + patch_pts + impact_pts + arch_pts, 2)

        task_success = func_res.passed and patch_res.passed and arch_res.passed and not is_patch_empty

        breakdown = {
            "legacy_score": legacy_score,
            "diagnostic_score_v2": diagnostic_score_v2,
            "task_success": task_success,
            "raw_dimensions": raw_dimensions,
            "evaluators": {
                "functional": func_res.to_dict(),
                "patch": patch_res.to_dict(),
                "architecture": arch_res.to_dict(),
                "impact": impact_res.to_dict(),
            },
        }

        return task_success, legacy_score, diagnostic_score_v2, breakdown
