"""CLI for Night Operations in Project Brain v0.5.3."""

import argparse
import json
from brain.operations.nightly import NightlyOperationsManager


def main():
    parser = argparse.ArgumentParser(description="Project Brain Night Operations CLI")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("nightly", help="Generate single night digest")
    subparsers.add_parser("incidents", help="List active correlated incidents")
    subparsers.add_parser("freshness", help="Show stale repository freshness reports")

    args = parser.parse_args()
    mgr = NightlyOperationsManager()

    if args.command == "nightly":
        digest = mgr.generate_single_digest()
        print(json.dumps(digest.model_dump(), indent=2))
    elif args.command == "incidents":
        print(json.dumps([i.model_dump() for i in mgr._incidents.values()], indent=2))
    elif args.command == "freshness":
        print(json.dumps([f.model_dump() for f in mgr._freshness.values()], indent=2))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
