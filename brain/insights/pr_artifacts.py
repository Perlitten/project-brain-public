"""GitHub Check Annotations and Environment Outputs Generator (Workstream E)."""

from __future__ import annotations

from typing import Any, List

from brain.insights.drift_enforcement import GuardEvaluationResult
from brain.insights.pr_risk import ArchitectureRiskAssessment
from brain.insights.pr_summary import PRSummaryModel


def generate_github_annotations(res: GuardEvaluationResult, max_annotations: int = 50) -> List[dict[str, Any]]:
    """Generate GitHub Check Annotations JSON structure."""
    annotations: List[dict[str, Any]] = []

    for item in res.evaluated_findings[:max_annotations]:
        f = item.finding
        level = "failure" if f.severity == "critical" else "warning" if f.severity == "warning" else "notice"

        annotations.append(
            {
                "path": f.file_path,
                "start_line": f.line_number or 1,
                "end_line": f.line_number or 1,
                "annotation_level": level,
                "title": f"Architectural Drift [{f.rule_id}]: {f.rule_name}",
                "message": f"{f.description} (State: {item.delta_state}, Waiver: {item.waiver_status})",
                "raw_details": f.remediation_guidance,
            }
        )

    return annotations


def generate_github_outputs_text(summary: PRSummaryModel, risk: ArchitectureRiskAssessment) -> str:
    """Generate sanitized GitHub Actions output key-value strings ($GITHUB_OUTPUT format)."""
    lines = [
        f"decision={summary.decision}",
        f"exit_code={summary.exit_code}",
        f"risk_category={risk.risk_category}",
        f"risk_score={risk.numeric_score}",
        f"new_critical_count={summary.new_findings_count}",
        f"resolved_count={summary.resolved_findings_count}",
        f"waived_count={summary.waived_count}",
    ]
    return "\n".join(lines)
