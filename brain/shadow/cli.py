"""CLI for Shadow Execution in Project Brain v0.5.3."""

import argparse
import json
from brain.shadow.runner import ShadowRunner


def main():
    parser = argparse.ArgumentParser(description="Project Brain Shadow Execution CLI")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("list", help="List shadow sessions")
    show_parser = subparsers.add_parser("show", help="Show shadow session details")
    show_parser.add_argument("shadow_id", help="Shadow session ID")

    subparsers.add_parser("summary", help="Show shadow execution summary")
    clean_parser = subparsers.add_parser("clean", help="Clean shadow session")
    clean_parser.add_argument("shadow_id", help="Shadow session ID")

    args = parser.parse_args()
    runner = ShadowRunner()

    if args.command == "list":
        sessions = runner.list_sessions()
        print(f"Total shadow sessions: {len(sessions)}")
    elif args.command == "show":
        sess = runner._sessions.get(args.shadow_id)
        if sess:
            print(json.dumps(sess.model_dump(), indent=2))
        else:
            print(f"Shadow session {args.shadow_id} not found")
    elif args.command == "summary":
        print(json.dumps(runner.get_summary(), indent=2))
    elif args.command == "clean":
        runner.cancel_session(args.shadow_id)
        print(f"Cleaned shadow session {args.shadow_id}")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
