"""Promotion Decision Report Generator for Project Brain v0.8.0."""

import json
from pathlib import Path
from typing import Dict, Optional
from brain.improvement.models import PairedEvaluationReport, CandidateHypothesis
from brain.config.paths import reports_dir


class PromotionReportGenerator:
    """Formats human-reviewable markdown and JSON promotion decision records."""

    def __init__(self, output_dir: Optional[Path] = None):
        self.output_dir = output_dir or reports_dir() / "improvement/promotions"
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate_report(
        self,
        hypothesis: CandidateHypothesis,
        evaluation_report: PairedEvaluationReport,
    ) -> Dict[str, Path]:
        """Generates both JSON and Markdown promotion decision records."""
        base_name = f"promotion-decision-{hypothesis.candidate_id}"
        json_path = self.output_dir / f"{base_name}.json"
        md_path = self.output_dir / f"{base_name}.md"

        data = {
            "hypothesis": hypothesis.model_dump(),
            "evaluation": evaluation_report.model_dump(),
        }
        json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

        rejection_bullets = "\n".join(f"- {r}" for r in evaluation_report.gate_rejections) if evaluation_report.gate_rejections else "- None (All gates passed)"

        md_content = f"""# Promotion Decision Record: {hypothesis.candidate_id}

## Executive Summary
- **Outcome**: `{evaluation_report.outcome.value}`
- **Challenger Bundle ID**: `{evaluation_report.challenger_bundle_id}`
- **Champion Bundle ID**: `{evaluation_report.champion_bundle_id}`
- **Evaluation Pool**: `{evaluation_report.evaluation_pool_type.value}`
- **McNemar Test p-value**: `{evaluation_report.mcnemar_p_value:.4f}`
- **95% Success Rate Diff CI**: `[{evaluation_report.success_rate_difference_ci[0]:.4f}, {evaluation_report.success_rate_difference_ci[1]:.4f}]`
- **Non-Inferiority Gate Passed**: `{evaluation_report.non_inferiority_passed}`

## Candidate Hypothesis
- **Hypothesis**: {hypothesis.hypothesis}
- **Lever Changes**: `{json.dumps(hypothesis.lever_changes)}`
- **Target Failure Clusters**: `{hypothesis.failure_cluster_ids}`

## Gate Rejection Reasons
{rejection_bullets}
"""
        md_path.write_text(md_content, encoding="utf-8")
        return {"json": json_path, "markdown": md_path}
