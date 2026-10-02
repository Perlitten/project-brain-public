"""Master Evaluator Suite for Utility Benchmark Pilot.

Combines Functional, Patch, Architecture, and Impact evaluators to calculate
binary task success and composite 0-100 score.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Tuple
from benchmarks.utility_pilot.evaluators.arch_eval import ArchEvaluator
from benchmarks.utility_pilot.evaluators.functional import FunctionalEvaluator
from benchmarks.utility_pilot.evaluators.impact_eval import ImpactEvaluator
from benchmarks.utility_pilot.evaluators.patch_eval import PatchEvaluator
from benchmarks.utility_pilot.schemas.run_model import GoldManifest


class EvaluatorSuite:
    """Master evaluator harness running all evaluators for a run."""

    @staticmethod
    def evaluate_run(
        workspace_dir: Path,
        patch_diff: str,
        files_read: List[str],
        gold: GoldManifest,
    ) -> Tuple[bool, float, Dict[str, Dict[str, Any]]]:
        """Run all evaluators.

        Returns (task_success, task_score, evaluator_results_dict).
        """
        func_res = FunctionalEvaluator.evaluate(workspace_dir, gold)
        patch_res = PatchEvaluator.evaluate(patch_diff, gold)
        arch_res = ArchEvaluator.evaluate(workspace_dir, gold)
        impact_res = ImpactEvaluator.evaluate(files_read, patch_diff, gold)

        results_dict = {
            "functional": func_res.to_dict(),
            "patch": patch_res.to_dict(),
            "architecture": arch_res.to_dict(),
            "impact": impact_res.to_dict(),
        }

        # Weighting: Functional (50%), Patch (20%), Architecture (20%), Impact (10%)
        weighted_score = round(
            (func_res.score * 0.50) +
            (patch_res.score * 0.20) +
            (arch_res.score * 0.20) +
            (impact_res.score * 0.10),
            2
        )

        # Binary task success requires functional pass AND patch pass AND arch pass
        task_success = func_res.passed and patch_res.passed and arch_res.passed

        return task_success, weighted_score, results_dict
