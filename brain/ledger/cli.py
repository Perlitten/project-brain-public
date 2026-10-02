"""Evidence ledger CLI — Phase F5.

    python3 -m brain.ledger.cli verify
    python3 -m brain.ledger.cli export --output ledger.jsonl
    python3 -m brain.ledger.cli show <event-id>
    python3 -m brain.ledger.cli entity-history <entity-type> <entity-id>

Read and verify only. The ledger is written by product actions, not by an
operator typing events into it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, List, Optional

from brain.ledger.ledger import EvidenceLedger
from brain.ledger.store import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))


def _ledger(args: argparse.Namespace) -> EvidenceLedger:
    return EvidenceLedger(Path(args.brain_dir))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m brain.ledger.cli",
        description="Inspect and verify the Project Brain evidence ledger",
    )
    parser.add_argument("--brain-dir", default=".brain", help="Brain state directory")
    sub = parser.add_subparsers(dest="command", required=True)

    verify = sub.add_parser("verify", help="Verify chain integrity")
    verify.add_argument(
        "--input", default="", help="Verify a JSONL export instead of the database"
    )

    export = sub.add_parser("export", help="Write a deterministic JSONL export")
    export.add_argument("--output", required=True, help="Destination .jsonl path")

    show = sub.add_parser("show", help="Show a single event")
    show.add_argument("event_id")

    history = sub.add_parser("entity-history", help="Show every event for one entity")
    history.add_argument("entity_type")
    history.add_argument("entity_id")
    history.add_argument("--limit", type=int, default=MAX_PAGE_SIZE)
    history.add_argument("--offset", type=int, default=0)

    events = sub.add_parser("events", help="Bounded, paginated event query")
    events.add_argument("--event-type", default="")
    events.add_argument("--repository-id", default="")
    events.add_argument("--entity-type", default="")
    events.add_argument("--entity-id", default="")
    events.add_argument("--actor", default="")
    events.add_argument("--since-sequence", type=int, default=0)
    events.add_argument("--limit", type=int, default=DEFAULT_PAGE_SIZE)
    events.add_argument("--offset", type=int, default=0)

    sub.add_parser("health", help="Compact integrity summary")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "verify":
        if args.input:
            path = Path(args.input)
            if not path.is_file():
                _print_json({"error": f"Export not found: {path}"})
                return 1
            report = EvidenceLedger.verify_export(path)
            report["source"] = str(path)
        else:
            report = _ledger(args).verify()
            report["source"] = "database"
        _print_json(report)
        # A tampered chain must fail the shell, not merely print prose.
        return 0 if report["valid"] else 1

    if args.command == "export":
        _print_json(_ledger(args).export(Path(args.output)))
        return 0

    if args.command == "show":
        event = _ledger(args).get(args.event_id)
        if event is None:
            _print_json({"error": f"Unknown event: {args.event_id}"})
            return 1
        payload = event.to_dict()
        payload["hash_valid"] = event.compute_hash() == event.event_hash
        _print_json(payload)
        return 0

    if args.command == "entity-history":
        result = _ledger(args).entity_history(
            args.entity_type, args.entity_id, limit=args.limit, offset=args.offset
        )
        result["entity_type"] = args.entity_type
        result["entity_id"] = args.entity_id
        _print_json(result)
        return 0

    if args.command == "events":
        _print_json(
            _ledger(args).query(
                event_type=args.event_type,
                repository_id=args.repository_id,
                entity_type=args.entity_type,
                entity_id=args.entity_id,
                actor_identity=args.actor,
                since_sequence=args.since_sequence,
                limit=args.limit,
                offset=args.offset,
            )
        )
        return 0

    if args.command == "health":
        health = _ledger(args).health()
        _print_json(health)
        return 0 if health["valid"] else 1

    return 1


if __name__ == "__main__":
    sys.exit(main())
