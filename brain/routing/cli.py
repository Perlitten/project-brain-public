"""CLI for Selective Routing in Project Brain v0.5.3."""

import argparse
import json
from brain.routing.models import RoutingContext, PolicyMode
from brain.routing.policy_engine import SelectivePolicyEngine


def main():
    parser = argparse.ArgumentParser(description="Project Brain Selective Routing CLI")
    subparsers = parser.add_subparsers(dest="command")

    assess_parser = subparsers.add_parser("assess", help="Assess task routing")
    assess_parser.add_argument("--category", default="general", help="Task category")
    assess_parser.add_argument("--complexity", default="moderate", help="Task complexity")
    assess_parser.add_argument("--files", type=int, default=1, help="File count")

    explain_parser = subparsers.add_parser("explain", help="Explain routing decision")
    explain_parser.add_argument("--category", default="architecture_boundary", help="Task category")

    args = parser.parse_args()

    engine = SelectivePolicyEngine()
    if args.command == "assess":
        ctx = RoutingContext(
            task_category=args.category,
            task_complexity=args.complexity,
            file_count=args.files,
            operator_mode=PolicyMode.OBSERVE,
        )
        decision = engine.evaluate(ctx)
        print(f"Selected Route: {decision.selected_route.value}")
        print(f"Brain Route: {decision.brain_route}")
    elif args.command == "explain":
        ctx = RoutingContext(
            task_category=args.category,
            architecture_sensitivity=(args.category == "architecture_boundary"),
            operator_mode=PolicyMode.OBSERVE,
        )
        decision = engine.evaluate(ctx)
        print(json.dumps(decision.model_dump(), indent=2))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
