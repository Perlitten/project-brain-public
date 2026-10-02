"""CLI Commands for Project Brain v0.5.2 Phased Agent Execution Loop."""

import argparse
from brain.execution.models import ExecutionState


def main():
    parser = argparse.ArgumentParser(description="Project Brain Execution Loop CLI")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Run a phased execution session")
    run_parser.add_argument("--repository", required=True, help="Repository ID")
    run_parser.add_argument("--task-file", required=True, help="Path to task JSON")

    show_parser = subparsers.add_parser("show", help="Show execution session status")
    show_parser.add_argument("execution_id", help="Execution Session ID")

    args = parser.parse_args()
    if args.command == "run":
        print(f"Started phased execution session for repo {args.repository}")
    elif args.command == "show":
        print(f"Session {args.execution_id}: State = {ExecutionState.RECEIVED.value}")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
