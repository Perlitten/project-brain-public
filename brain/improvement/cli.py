"""Project Brain Continuous Improvement Platform CLI Tool (v0.8.0)."""

import argparse
import json
import sys
from brain.improvement.registry.registry import BundleRegistry
from brain.improvement.safety_mutations import SafetyMutationAuditor
from brain.improvement.weekly_factory import WeeklyImprovementFactory


def run_improvement_status() -> int:
    """Prints current champion bundle and improvement platform status."""
    registry = BundleRegistry()
    auditor = SafetyMutationAuditor()
    all_killed, killed, total, failures = auditor.audit_safety_mutations()

    champ_id = registry.get_alias("champion")
    prev_id = registry.get_alias("previous_champion")
    challenger_id = registry.get_alias("challenger")

    print("=== PROJECT BRAIN AUTOMATED HYPOTHESIS FACTORY v0.8.0 ===")
    print(f"  Champion Bundle:           {champ_id or 'none'}")
    print(f"  Previous Champion Bundle:  {prev_id or 'none'}")
    print(f"  Current Challenger Bundle: {challenger_id or 'none'}")
    print(f"  Safety Mutation Kill Rate: {killed}/{total} ({ (killed/total)*100.0:.1f}% )")
    print("  System Classification:     L1 — Human feedback and operational state recorded")
    print("  Training Eligibility:      DISABLED (training_eligible=False by default)")
    print("\nStatus: FACTORY_OPERATIONAL_HUMAN_GATED")
    return 0


def main():
    parser = argparse.ArgumentParser(prog="brain.improvement")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("status", help="Show current improvement factory status")
    subparsers.add_parser("bundles", help="List registered agent bundles")
    parser_weekly = subparsers.add_parser("run-weekly-factory", help="Run 5-phase weekly improvement cycle")
    parser_weekly.add_argument("--cluster-id", default="cluster-premature-edit", help="Target failure cluster ID")

    parser_tournament = subparsers.add_parser("run-tournament", help="Run multi-candidate tournament sandbox bracket")
    parser_tournament.add_argument("--clusters", nargs="+", default=["cluster-premature-edit", "cluster-context-overflow"], help="Target failure cluster IDs")

    args = parser.parse_args()
    if args.command == "status":
        sys.exit(run_improvement_status())
    elif args.command == "bundles":
        reg = BundleRegistry()
        print("Registered Aliases:", json.dumps(reg._aliases, indent=2))
        sys.exit(0)
    elif args.command == "run-weekly-factory":
        wf = WeeklyImprovementFactory()
        res = wf.run_weekly_cycle(target_cluster_id=args.cluster_id)
        print(json.dumps(res, indent=2))
        sys.exit(0)
    elif args.command == "run-tournament":
        from brain.improvement.tournament.engine import MultiAgentTournamentEngine
        engine = MultiAgentTournamentEngine()
        res = engine.run_tournament(target_cluster_ids=args.clusters)
        print(json.dumps(res.model_dump(), indent=2))
        sys.exit(0)
    else:
        sys.exit(run_improvement_status())


if __name__ == "__main__":
    main()
