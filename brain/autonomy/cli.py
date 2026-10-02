"""CLI interface for Production GoalRun Service (v0.9.0).

Usage:
  python -m brain.autonomy.cli submit-goal --goal-id <id> --description "<desc>" [--target-file <path>]
  python -m brain.autonomy.cli goal-status --goal-id <id>
  python -m brain.autonomy.cli cancel-goal --goal-id <id>
"""
import argparse
import asyncio
import json
import sys
from brain.autonomy.goal_run import GoalRunService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="brain.autonomy", description="Production GoalRun Service CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    submit = sub.add_parser("submit-goal", help="Submit a new goal to the work queue")
    submit.add_argument("--goal-id", required=True, help="Unique Goal ID")
    submit.add_argument("--description", required=True, help="Natural language goal description")
    submit.add_argument("--target-file", default=None, help="Target file path")
    submit.add_argument("--requires-deploy", action="store_true", help="Set if goal requires VPS deploy")

    status = sub.add_parser("goal-status", help="Get status of a submitted goal")
    status.add_argument("--goal-id", required=True, help="Goal ID to query")

    cancel = sub.add_parser("cancel-goal", help="Cancel a submitted goal")
    cancel.add_argument("--goal-id", required=True, help="Goal ID to cancel")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    service = GoalRunService()

    if args.command == "submit-goal":
        asyncio.run(service.submit_goal(
            goal_id=args.goal_id,
            description=args.description,
            target_file=args.target_file,
            requires_deploy=args.requires_deploy,
        ))
        executed = asyncio.run(service.execute_goal_run(args.goal_id))
        print(json.dumps({
            "status": "ok",
            "goal_id": executed.goal_id,
            "phase": executed.current_phase.value,
            "commit_sha": executed.commit_sha,
            "deployed": executed.deployed,
            "worktree_path": executed.worktree_path,
        }, indent=2))

    elif args.command == "goal-status":
        status_run = service.get_goal_status(args.goal_id)
        if not status_run:
            print(json.dumps({"error": f"Goal {args.goal_id} not found"}, indent=2))
            return 1
        print(json.dumps({
            "goal_id": status_run.goal_id,
            "phase": status_run.current_phase.value,
            "commit_sha": status_run.commit_sha,
            "deployed": status_run.deployed,
            "worktree_path": status_run.worktree_path,
        }, indent=2))

    elif args.command == "cancel-goal":
        success = service.cancel_goal(args.goal_id)
        print(json.dumps({"status": "cancelled" if success else "not_found", "goal_id": args.goal_id}, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
