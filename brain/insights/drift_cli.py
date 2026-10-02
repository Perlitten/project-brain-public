"""Production CLI tool for Architectural Change Guard (Phase 7).

Usage:
  python3 -m brain.insights.drift_cli scan <repo_path>
  python3 -m brain.insights.drift_cli summary <repo_path>
  python3 -m brain.insights.drift_cli check --repo <path> --base <base> --candidate <candidate> --format <text|json|sarif> --output <file>
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from brain.insights.codeowners import CodeownersParser, FindingOwnershipResolver
from brain.insights.drift_analyzer import scan_repository_drift
from brain.insights.drift_baseline import DriftBaseline, DriftBaselineManager
from brain.insights.drift_enforcement import ArchitectureChangeGuardEngine, GuardEvaluationResult
from brain.insights.drift_rules import get_default_rule_registry
from brain.insights.pr_artifacts import generate_github_annotations, generate_github_outputs_text
from brain.insights.pr_risk import ArchitectureRiskCalculator
from brain.insights.pr_summary import PRMarkdownRenderer, PRSummaryModel


def generate_sarif_report(result: GuardEvaluationResult) -> dict[str, Any]:
    """Generate SARIF 2.1.0 compliant JSON schema report."""
    rules = get_default_rule_registry().list_active_rules()
    sarif_rules = [
        {
            "id": r.rule_id,
            "name": r.name,
            "shortDescription": {"text": r.description},
            "fullDescription": {"text": r.remediation_guidance},
            "defaultConfiguration": {"level": "error" if r.severity == "critical" else "warning"},
        }
        for r in rules
    ]

    results = []
    for ef in result.evaluated_findings:
        f = ef.finding
        level = "error" if f.severity == "critical" else "warning" if f.severity == "warning" else "note"
        results.append(
            {
                "ruleId": f.rule_id,
                "level": level,
                "message": {"text": f"{f.description} (State: {ef.delta_state}, Waiver: {ef.waiver_status})"},
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {"uri": f.file_path},
                            "region": {"startLine": f.line_number or 1},
                        }
                    }
                ],
                "properties": {
                    "fingerprint": f.fingerprint,
                    "containing_symbol": f.containing_symbol,
                    "imported_module": f.imported_module,
                    "delta_state": ef.delta_state,
                    "waiver_status": ef.waiver_status,
                },
            }
        )

    return {
        "$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "Project Brain Architecture Change Guard",
                        "version": "0.3.0",
                        "rules": sarif_rules,
                    }
                },
                "results": results,
            }
        ],
    }


def check_cmd(args: argparse.Namespace) -> int:
    repo_path = Path(args.repo).resolve()
    engine = ArchitectureChangeGuardEngine(repo_path)
    res = engine.evaluate_change_guard(base_rev=args.base, candidate_rev=args.candidate)

    if args.format == "json":
        output_data = json.dumps(res.to_dict(), indent=2)
    elif args.format == "sarif":
        output_data = json.dumps(generate_sarif_report(res), indent=2)
    else:
        # Text format
        lines = [
            f"=== Architectural Change Guard ({res.decision.upper()}) ===",
            f"Mode: {res.mode} | Base: {res.base_revision} -> Candidate: {res.candidate_revision}",
            f"Files Changed: {res.files_changed_count} | Scanned: {res.files_scanned_count}",
            f"Waived Findings: {res.waived_count} | Unwaived Findings: {res.unwaived_count}",
        ]
        if res.policy_reasons:
            lines.append("Policy Failure Reasons:")
            for reason in res.policy_reasons:
                lines.append(f"  - {reason}")
        output_data = "\n".join(lines)

    if args.output:
        out_path = Path(args.output).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output_data, encoding="utf-8")
        print(f"Report written to {out_path}")
    else:
        print(output_data)

    return res.exit_code


def scan_cmd(repo_path_str: str) -> None:
    repo_path = Path(repo_path_str).resolve()
    print(f"Scanning repository for architectural drift: {repo_path}")
    start = time.time()
    findings = scan_repository_drift(repo_path)

    baseline_mgr = DriftBaselineManager(repo_path / ".brain" / "drift_baseline.json")
    prev_baseline = baseline_mgr.load_baseline()
    evaluated = baseline_mgr.compute_deltas(findings, prev_baseline)

    new_baseline = DriftBaseline(
        repository_id=repo_path.name,
        source_revision="HEAD",
        created_at_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        rule_registry_version="1.0.0",
        findings=[e.finding.to_dict() for e in evaluated if e.delta_state != "resolved"],
    )
    baseline_mgr.save_baseline_atomic(new_baseline)
    elapsed = round(time.time() - start, 3)

    summary = {
        "status": "success",
        "repository": repo_path.as_posix(),
        "total_active_violations": len(new_baseline.findings),
        "duration_seconds": elapsed,
        "deltas": {
            "new": len([e for e in evaluated if e.delta_state == "new"]),
            "persistent": len([e for e in evaluated if e.delta_state == "persistent"]),
            "moved": len([e for e in evaluated if e.delta_state == "moved"]),
            "resolved": len([e for e in evaluated if e.delta_state == "resolved"]),
        },
    }
    print(json.dumps(summary, indent=2))


def summary_cmd(repo_path_str: str) -> None:
    repo_path = Path(repo_path_str).resolve()
    baseline_mgr = DriftBaselineManager(repo_path / ".brain" / "drift_baseline.json")
    baseline = baseline_mgr.load_baseline()
    if not baseline:
        print(json.dumps({"status": "no_baseline", "message": "No baseline scan found."}, indent=2))
        return

    summary = {
        "status": "success",
        "repository": baseline.repository_id,
        "source_revision": baseline.source_revision,
        "last_scan_utc": baseline.created_at_utc,
        "total_violations": len(baseline.findings),
        "rule_registry_version": baseline.rule_registry_version,
    }
    print(json.dumps(summary, indent=2))


def pr_review_cmd(args: argparse.Namespace) -> int:
    repo_path = Path(args.repo).resolve()
    engine = ArchitectureChangeGuardEngine(repo_path)
    res = engine.evaluate_change_guard(base_rev=args.base, candidate_rev=args.candidate)

    # CODEOWNERS resolution
    codeowners = CodeownersParser.discover_and_parse(repo_path)
    resolver = FindingOwnershipResolver(codeowners)

    owners_set = set()
    unowned_count = 0

    for ef in res.evaluated_findings:
        ownership = resolver.resolve_path_ownership(ef.finding.file_path)
        if ownership.is_unowned:
            unowned_count += 1
        owners_set.update(ownership.owners)

    # Risk Assessment
    risk = ArchitectureRiskCalculator.calculate_risk(
        new_criticals_count=len([e for e in res.evaluated_findings if e.delta_state == "new" and e.finding.severity == "critical"]),
        new_warnings_count=len([e for e in res.evaluated_findings if e.delta_state == "new" and e.finding.severity == "warning"]),
        expired_waivers_count=len([e for e in res.evaluated_findings if e.waiver_status == "waiver_expired"]),
        unowned_findings_count=unowned_count,
        files_changed_count=res.files_changed_count,
        policy_failed=(res.decision == "fail"),
    )

    # Summary Model & Markdown
    summary_model = PRSummaryModel(
        repository=repo_path.name,
        base_revision=res.base_revision,
        candidate_revision=res.candidate_revision,
        decision=res.decision,
        exit_code=res.exit_code,
        policy_version="1.0.0",
        files_changed=res.files_changed_count,
        files_scanned=res.files_scanned_count,
        new_findings_count=res.counts_by_state.get("new", 0),
        persistent_findings_count=res.counts_by_state.get("persistent", 0),
        moved_findings_count=res.counts_by_state.get("moved", 0),
        resolved_findings_count=res.counts_by_state.get("resolved", 0),
        waived_count=res.waived_count,
        expired_waivers_count=len([e for e in res.evaluated_findings if e.waiver_status == "waiver_expired"]),
        risk_score=risk.numeric_score,
        risk_category=risk.risk_category,
        risk_reasons=risk.reasons,
        responsible_owners=sorted(list(owners_set)),
        unowned_findings_count=unowned_count,
        findings_details=[e.finding.to_dict() | {"delta_state": e.delta_state, "waiver_status": e.waiver_status} for e in res.evaluated_findings],
    )

    rendered_md = PRMarkdownRenderer.render_comment(summary_model)

    if args.output_dir:
        out_dir = Path(args.output_dir).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)

        (out_dir / "pr-comment.md").write_text(rendered_md, encoding="utf-8")
        (out_dir / "result.json").write_text(json.dumps(res.to_dict(), indent=2), encoding="utf-8")
        (out_dir / "risk.json").write_text(json.dumps(risk.to_dict(), indent=2), encoding="utf-8")
        (out_dir / "annotations.json").write_text(json.dumps(generate_github_annotations(res), indent=2), encoding="utf-8")
        (out_dir / "github-outputs.txt").write_text(generate_github_outputs_text(summary_model, risk), encoding="utf-8")
        print(f"PR Review Package written to {out_dir}")
    else:
        print(rendered_md)

    return res.exit_code


def main() -> None:
    parser = argparse.ArgumentParser(description="Architectural Change Guard CLI")
    subparsers = parser.add_subparsers(dest="subcommand")

    # scan
    scan_p = subparsers.add_parser("scan")
    scan_p.add_argument("repo_path", help="Path to target repository")

    # summary
    sum_p = subparsers.add_parser("summary")
    sum_p.add_argument("repo_path", help="Path to target repository")

    # check
    check_p = subparsers.add_parser("check")
    check_p.add_argument("--repo", default=".", help="Path to target repository")
    check_p.add_argument("--base", default="HEAD~1", help="Base revision")
    check_p.add_argument("--candidate", default="HEAD", help="Candidate revision")
    check_p.add_argument("--format", choices=["text", "json", "sarif"], default="text", help="Output format")
    check_p.add_argument("--output", help="Output file path")

    # pr-review
    pr_p = subparsers.add_parser("pr-review")
    pr_p.add_argument("--repo", default=".", help="Path to target repository")
    pr_p.add_argument("--base", default="HEAD~1", help="Base revision")
    pr_p.add_argument("--candidate", default="HEAD", help="Candidate revision")
    pr_p.add_argument("--output-dir", help="Output directory for PR review package")

    args = parser.parse_args()

    if args.subcommand == "scan":
        scan_cmd(args.repo_path)
        sys.exit(0)
    elif args.subcommand == "summary":
        summary_cmd(args.repo_path)
        sys.exit(0)
    elif args.subcommand == "check":
        code = check_cmd(args)
        sys.exit(code)
    elif args.subcommand == "pr-review":
        code = pr_review_cmd(args)
        sys.exit(code)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
