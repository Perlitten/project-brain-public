"""Deterministic Architecture Risk Engine for PR Review Intelligence (Workstream C)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, List


@dataclass
class ArchitectureRiskAssessment:
    numeric_score: float  # 0.0 to 100.0
    risk_category: str  # 'low', 'medium', 'high', 'critical'
    reasons: List[str]
    formula_version: str = "1.0.0"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ArchitectureRiskCalculator:
    """Computes a deterministic, non-LLM architecture risk score."""

    @classmethod
    def calculate_risk(
        self,
        new_criticals_count: int,
        new_warnings_count: int,
        expired_waivers_count: int,
        unowned_findings_count: int,
        files_changed_count: int,
        policy_failed: bool,
        protected_rule_violated: bool = False,
    ) -> ArchitectureRiskAssessment:
        score = 0.0
        reasons: List[str] = []

        # 1. New Critical Violations (+35 per finding)
        if new_criticals_count > 0:
            points = min(70.0, new_criticals_count * 35.0)
            score += points
            reasons.append(f"+{points:.1f} points: {new_criticals_count} new critical architectural violation(s)")

        # 2. New Warnings (+10 per warning)
        if new_warnings_count > 0:
            points = min(30.0, new_warnings_count * 10.0)
            score += points
            reasons.append(f"+{points:.1f} points: {new_warnings_count} new architectural warning(s)")

        # 3. Expired Waivers (+20 per expired waiver)
        if expired_waivers_count > 0:
            points = min(40.0, expired_waivers_count * 20.0)
            score += points
            reasons.append(f"+{points:.1f} points: {expired_waivers_count} expired waiver(s) still active")

        # 4. Unowned Findings (+15 per unowned finding)
        if unowned_findings_count > 0:
            points = min(30.0, unowned_findings_count * 15.0)
            score += points
            reasons.append(f"+{points:.1f} points: {unowned_findings_count} finding(s) lack assigned CODEOWNERS")

        # 5. File Change Blast Radius
        if files_changed_count > 20:
            score += 15.0
            reasons.append(f"+15.0 points: Large change blast radius ({files_changed_count} files changed)")
        elif files_changed_count > 10:
            score += 5.0
            reasons.append(f"+5.0 points: Moderate change blast radius ({files_changed_count} files changed)")

        # Policy Failures & Protected Rules Elevate Score Minimums
        if protected_rule_violated:
            score = max(score, 85.0)
            reasons.append("Protected architectural rule boundary violated")

        if policy_failed:
            score = max(score, 75.0)
            reasons.append("Repository architecture policy enforcement failed")

        final_score = min(100.0, round(score, 1))

        if final_score >= 75.0:
            category = "critical"
        elif final_score >= 50.0:
            category = "high"
        elif final_score >= 25.0:
            category = "medium"
        else:
            category = "low"

        if not reasons:
            reasons.append("Clean architectural change-set; no elevated risk factors detected")

        return ArchitectureRiskAssessment(
            numeric_score=final_score,
            risk_category=category,
            reasons=reasons,
        )
