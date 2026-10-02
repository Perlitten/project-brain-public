"""Architecture Evaluator for Utility Benchmark Pilot.

Evaluates whether the patch violates subsystem boundaries, ORM rules, or design guards
using DriftAnalyzer (DRIFT-001 through DRIFT-005).
"""

from __future__ import annotations

from pathlib import Path
from brain.insights.drift_analyzer import ArchitecturalDriftAnalyzer
from benchmarks.utility_pilot.schemas.run_model import EvaluatorResult, GoldManifest


class ArchEvaluator:
    """Evaluates architecture drift and compliance."""

    @staticmethod
    def evaluate(workspace_dir: Path, gold: GoldManifest) -> EvaluatorResult:
        violations = []
        details = {}
        score = 100.0

        try:
            analyzer = ArchitecturalDriftAnalyzer(workspace_dir)
            drift_findings = []
            # Scan repo files
            for p in workspace_dir.rglob("*.py"):
                if any(part.startswith(".") or part in ("venv", "node_modules") for part in p.parts):
                    continue
                try:
                    rel_p = p.relative_to(workspace_dir).as_posix()
                    code = p.read_text(encoding="utf-8", errors="ignore")
                    drift_findings.extend(analyzer.analyze_file(rel_p, code))
                except Exception:
                    pass

            details["total_findings"] = len(drift_findings)
            details["findings"] = [f.to_dict() if hasattr(f, "to_dict") else str(f) for f in drift_findings]

            # Check if any new violations violate gold arch constraints
            for constraint in gold.arch_constraints:
                # e.g., "DRIFT-001: no direct ORM usage in API layer"
                rule_id = constraint.split(":")[0].strip() if ":" in constraint else constraint
                matching = [f for f in drift_findings if getattr(f, "rule_id", "") == rule_id or rule_id in str(f)]
                if matching:
                    violations.append(f"Architecture constraint violated: {constraint} ({len(matching)} instances)")
                    score -= 35.0

            # Deduct for general high/critical findings
            for f in drift_findings:
                sev = getattr(f, "severity", "medium").lower()
                if sev == "critical":
                    violations.append(f"Critical architecture drift: {getattr(f, 'description', str(f))}")
                    score -= 25.0
                elif sev == "high":
                    violations.append(f"High architecture drift: {getattr(f, 'description', str(f))}")
                    score -= 15.0

        except Exception as exc:
            violations.append(f"Architecture analyzer failure: {exc}")
            score -= 10.0

        score = max(0.0, round(score, 2))
        passed = len(violations) == 0

        return EvaluatorResult(
            evaluator_name="architecture",
            passed=passed,
            score=score,
            details=details,
            violations=violations,
        )
