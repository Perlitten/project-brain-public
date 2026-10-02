"""Generate Phase 13 Real Repository Verification Reports for Architectural Change Guard."""

import json
from pathlib import Path
from brain.insights.drift_enforcement import ArchitectureChangeGuardEngine
from brain.insights.drift_cli import generate_sarif_report

def main():
    repo_root = Path(__file__).resolve().parent.parent
    out_dir = repo_root / "reports" / "v0.3.0-architecture-change-guard"
    out_dir.mkdir(parents=True, exist_ok=True)

    engine = ArchitectureChangeGuardEngine(repo_root)
    # Check HEAD~1 against HEAD
    res = engine.evaluate_change_guard(base_rev="HEAD~1", candidate_rev="HEAD")

    # JSON report
    json_path = out_dir / "project-brain-check.json"
    json_path.write_text(json.dumps(res.to_dict(), indent=2), encoding="utf-8")

    # SARIF report
    sarif_data = generate_sarif_report(res)
    sarif_path = out_dir / "project-brain-check.sarif"
    sarif_path.write_text(json.dumps(sarif_data, indent=2), encoding="utf-8")

    # Summary report
    summary_md = f"""# Architectural Change Guard Verification Summary (v0.3.0 Milestone)

## Execution Metadata
* **Repository**: Project Brain
* **Base Revision**: `{res.base_revision}`
* **Candidate Revision**: `{res.candidate_revision}`
* **Enforcement Mode**: `{res.mode}`
* **Enforcement Decision**: `{res.decision.upper()}`
* **CI Process Exit Code**: `{res.exit_code}`

## Scan Metrics
* **Files Changed**: `{res.files_changed_count}`
* **Python Files Scanned**: `{res.files_scanned_count}`
* **Waived Findings**: `{res.waived_count}`
* **Unwaived Findings**: `{res.unwaived_count}`

## Finding Breakdown by Severity
* **Critical**: `{res.counts_by_severity.get("critical", 0)}`
* **Warning**: `{res.counts_by_severity.get("warning", 0)}`
* **Info**: `{res.counts_by_severity.get("info", 0)}`

## Finding Breakdown by State
* **New**: `{res.counts_by_state.get("new", 0)}`
* **Persistent**: `{res.counts_by_state.get("persistent", 0)}`
* **Moved**: `{res.counts_by_state.get("moved", 0)}`
* **Resolved**: `{res.counts_by_state.get("resolved", 0)}`
"""
    (out_dir / "summary.md").write_text(summary_md, encoding="utf-8")
    print("Reports generated successfully in reports/v0.3.0-architecture-change-guard/")

if __name__ == "__main__":
    main()
