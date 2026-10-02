"""Functional Evaluator for Utility Benchmark Pilot.

Executes mandatory and hidden acceptance tests to verify solution correctness.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from benchmarks.utility_pilot.schemas.run_model import EvaluatorResult, GoldManifest


class FunctionalEvaluator:
    """Evaluates functional correctness by running required pytest commands."""

    @staticmethod
    def evaluate(workspace_dir: Path, gold: GoldManifest) -> EvaluatorResult:
        violations = []
        details = {}
        passed_tests = 0
        total_tests = len(gold.mandatory_tests)

        if total_tests == 0:
            return EvaluatorResult(
                evaluator_name="functional",
                passed=True,
                score=100.0,
                details={"passed_tests": 0, "total_tests": 0},
            )

        for test_cmd in gold.mandatory_tests:
            try:
                res = subprocess.run(
                    test_cmd,
                    shell=True,
                    cwd=str(workspace_dir),
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                details[test_cmd] = {
                    "exit_code": res.returncode,
                    "stdout_snippet": res.stdout[:500],
                    "stderr_snippet": res.stderr[:500],
                }
                if res.returncode == 0:
                    passed_tests += 1
                else:
                    violations.append(f"Test failed: '{test_cmd}' (exit code {res.returncode})")
            except subprocess.TimeoutExpired:
                violations.append(f"Test timed out: '{test_cmd}'")
                details[test_cmd] = {"exit_code": -1, "error": "timeout"}
            except Exception as exc:
                violations.append(f"Test execution error for '{test_cmd}': {exc}")
                details[test_cmd] = {"exit_code": -1, "error": str(exc)}

        score = round((passed_tests / total_tests) * 100.0, 2)
        all_passed = passed_tests == total_tests

        return EvaluatorResult(
            evaluator_name="functional",
            passed=all_passed,
            score=score,
            details=details,
            violations=violations,
        )
