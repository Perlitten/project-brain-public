"""CLI Interface for Graphify v2 Graph Operations."""

import argparse
import json
import sys
from pathlib import Path

from brain.graph.builder_v2 import GraphBuilderV2
from brain.graph.generation_manager import GraphGenerationManager


def main():
    parser = argparse.ArgumentParser(description="Project Brain Graphify v2 CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # build
    build_parser = subparsers.add_parser("build", help="Build a new graph generation")
    build_parser.add_argument("--repo", default=".", help="Repository root path")
    build_parser.add_argument("--rev", default="HEAD", help="Git revision")
    build_parser.add_argument("--activate", action="store_true", help="Automatically activate if quality gate passes")

    # list
    list_parser = subparsers.add_parser("list", help="List graph generations")
    list_parser.add_argument("--repo", default=".", help="Repository root path")

    # activate
    activate_parser = subparsers.add_parser("activate", help="Activate a graph generation")
    activate_parser.add_argument("--repo", default=".", help="Repository root path")
    activate_parser.add_argument("--gen-id", required=True, help="Generation ID to activate")

    # rollback
    rollback_parser = subparsers.add_parser("rollback", help="Rollback to prior active graph generation")
    rollback_parser.add_argument("--repo", default=".", help="Repository root path")

    args = parser.parse_args()

    repo_path = Path(args.repo).resolve()
    mgr = GraphGenerationManager(repo_path / ".brain")

    if args.command == "build":
        builder = GraphBuilderV2(repo_path)
        meta, report = builder.build_generation(git_revision=args.rev)
        print(f"Graph generation build complete: {meta.generation_id}")
        print(f"Nodes: {meta.node_count}, Relationships: {meta.relationship_count}")

        if args.activate:
            if mgr.activate_generation(meta.generation_id):
                print(f"Activated generation: {meta.generation_id}")
            else:
                print(f"Failed to activate generation: {meta.generation_id}")

    elif args.command == "list":
        gens = mgr.list_generations()
        active_id = mgr.get_active_generation_id()
        print(json.dumps({
            "active_generation_id": active_id,
            "generations": [g.to_dict() for g in gens]
        }, indent=2))

    elif args.command == "activate":
        if mgr.activate_generation(args.gen_id):
            print(f"Successfully activated generation: {args.gen_id}")
        else:
            print(f"Failed to activate generation: {args.gen_id}", file=sys.stderr)
            sys.exit(1)

    elif args.command == "rollback":
        rolled = mgr.rollback()
        if rolled:
            print(f"Successfully rolled back active generation to: {rolled}")
        else:
            print("No prior valid generation available for rollback.", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
