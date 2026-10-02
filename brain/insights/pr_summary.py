"""PR Comment Summary Renderer for Architectural Review Intelligence (Workstream A)."""

from __future__ import annotations

import html
import re
from dataclasses import asdict, dataclass, field
from typing import Any, List


COMMENT_MARKER = "<!-- project-brain:architecture-review:v1 -->"
MAX_COMMENT_CHARS = 10000


@dataclass
class PRSummaryModel:
    repository: str
    base_revision: str
    candidate_revision: str
    decision: str
    exit_code: int
    policy_version: str
    files_changed: int
    files_scanned: int
    new_findings_count: int
    persistent_findings_count: int
    moved_findings_count: int
    resolved_findings_count: int
    waived_count: int
    expired_waivers_count: int
    risk_score: float
    risk_category: str
    risk_reasons: List[str]
    responsible_owners: List[str]
    unowned_findings_count: int
    findings_details: List[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PRMarkdownRenderer:
    """Renders production-ready, sanitized Markdown comments for PR reviews."""

    @classmethod
    def render_comment(cls, summary: PRSummaryModel) -> str:
        dec_upper = summary.decision.upper()
        emoji = "✅" if summary.decision == "pass" else "⚠️" if summary.decision == "warn" else "❌"

        lines = [
            COMMENT_MARKER,
            f"## {emoji} Project Brain Architecture Review: **{dec_upper}**",
            f"**Repository**: `{html.escape(summary.repository)}` | **Revisions**: `{html.escape(summary.base_revision[:7])}` → `{html.escape(summary.candidate_revision[:7])}`",
            f"**Enforcement Mode**: `fail` | **CI Exit Code**: `{summary.exit_code}` | **Risk Rating**: `{summary.risk_category.upper()}` ({summary.risk_score}/100)",
            "",
            "### 📊 Change & Drift Overview",
            "| Metric | Count | Metric | Count |",
            "| :--- | :--- | :--- | :--- |",
            f"| **Files Changed** | `{summary.files_changed}` | **Python Scanned** | `{summary.files_scanned}` |",
            f"| **New Findings** | `{summary.new_findings_count}` | **Persistent** | `{summary.persistent_findings_count}` |",
            f"| **Moved Findings** | `{summary.moved_findings_count}` | **Resolved** | `{summary.resolved_findings_count}` |",
            f"| **Waived** | `{summary.waived_count}` | **Expired Waivers** | `{summary.expired_waivers_count}` |",
            "",
            "### 🎯 CODEOWNERS & Reviewer Routing",
            f"* **Assigned Owners**: {', '.join([f'`{html.escape(o)}`' for o in summary.responsible_owners]) if summary.responsible_owners else '`None`'}",
            f"* **Unowned Findings**: `{summary.unowned_findings_count}`",
            "",
        ]

        # Add Risk Reasons
        lines.append("### ⚡ Risk Assessment Drivers")
        for reason in summary.risk_reasons:
            lines.append(f"* {html.escape(reason)}")
        lines.append("")

        # Add Findings Details if any
        if summary.findings_details:
            lines.append("<details><summary><b>🔍 View Detailed Architectural Findings</b></summary>")
            lines.append("")
            lines.append("| Rule ID | Severity | State | File & Line | Message |")
            lines.append("| :--- | :--- | :--- | :--- | :--- |")

            for f in summary.findings_details[:20]:  # Limit top 20 for comment length safety
                rule_id = html.escape(str(f.get("rule_id", "")))
                sev = html.escape(str(f.get("severity", "")))
                st = html.escape(str(f.get("delta_state", "")))
                fpath = html.escape(str(f.get("file_path", "")))
                line_no = f.get("line_number", 1)
                msg = html.escape(str(f.get("description", "")))
                lines.append(f"| `{rule_id}` | `{sev}` | `{st}` | `{fpath}:{line_no}` | {msg} |")

            lines.append("</details>")
            lines.append("")

        lines.append("---")
        lines.append("_Generated automatically by Project Brain Architecture Review Intelligence (v0.3.0)._")

        full_md = "\n".join(lines)

        # Enforce max comment length safety
        if len(full_md) > MAX_COMMENT_CHARS:
            truncated_md = full_md[: MAX_COMMENT_CHARS - 200] + "\n\n**[Notice] Output truncated due to length limits. Full results available in attached SARIF/JSON artifacts.**"
            return truncated_md

        return full_md

    @classmethod
    def replace_or_append_comment(cls, existing_body: str, new_comment: str) -> str:
        """Replace existing Project Brain comment block or append deterministically."""
        if not existing_body:
            return new_comment

        pattern = re.compile(rf"{re.escape(COMMENT_MARKER)}.*?(?=\n#|\Z)", re.DOTALL)
        if pattern.search(existing_body):
            return pattern.sub(new_comment, existing_body)

        return existing_body.strip() + "\n\n" + new_comment
