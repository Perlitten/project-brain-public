"""CLI Interface for Assisted Remediation Planner."""

import argparse
import json
import sys
from pathlib import Path

from brain.insights.remediation_manager import RemediationPlanManager
from brain.insights.remediation_strategies import RemediationStrategyRegistry


def main():
    parser = argparse.ArgumentParser(description="Project Brain Remediation Planner CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # propose
    propose_p = subparsers.add_parser("propose", help="Propose a remediation plan for a finding")
    propose_p.add_argument("--repo", default=".", help="Repository root path")
    propose_p.add_argument("--rule-id", default="DRIFT-001", help="Rule ID")
    propose_p.add_argument("--file", required=True, help="Affected file path")
    propose_p.add_argument("--desc", default="Architectural finding detected", help="Problem description")

    # show
    show_p = subparsers.add_parser("show", help="Show a remediation plan")
    show_p.add_argument("--repo", default=".", help="Repository root path")
    show_p.add_argument("--plan-id", required=True, help="Plan ID")

    # validate
    val_p = subparsers.add_parser("validate", help="Validate plan freshness")
    val_p.add_argument("--repo", default=".", help="Repository root path")
    val_p.add_argument("--plan-id", required=True, help="Plan ID")

    args = parser.parse_args()

    repo_path = Path(args.repo).resolve()
    mgr = RemediationPlanManager(repo_path / ".brain")

    if args.command == "propose":
        finding_dict = {"rule_id": args.rule_id, "file_path": args.file, "description": args.desc}
        plan = RemediationStrategyRegistry.create_plan_for_finding(repo_path.name, "HEAD", finding_dict, repo_path)
        mgr.save_plan(plan)
        print(f"Proposed remediation plan: {plan.plan_id}")
        print(json.dumps(plan.to_dict(), indent=2))

    elif args.command == "show":
        plan = mgr.get_plan(args.plan_id)
        if not plan:
            print(f"Plan '{args.plan_id}' not found", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(plan.to_dict(), indent=2))

    elif args.command == "validate":
        fresh, reason = mgr.validate_plan_freshness(args.plan_id, repo_path)
        print(json.dumps({"fresh": fresh, "reason": reason}, indent=2))
        if not fresh:
            sys.exit(1)


if __name__ == "__main__":
    main()
