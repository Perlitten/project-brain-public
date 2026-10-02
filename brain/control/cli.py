"""Operator control plane CLI — Phases G1–G4.

    python3 -m brain.control.cli summary
    python3 -m brain.control.cli queue [--repository-id ID] [--action-type TYPE]
    python3 -m brain.control.cli budgets
    python3 -m brain.control.cli check-budget <budget> [--requested N]
    python3 -m brain.control.cli metrics
    python3 -m brain.control.cli health

Read-only. The queue tells an operator what needs a decision; it does not make
one, and nothing here executes a change against an authoritative repository.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, List, Optional

from brain.control.budgets import (
    BUDGET_FILE_NAME,
    BudgetExceededError,
    BudgetPolicy,
    BudgetUnknownError,
)
from brain.control.plane import ALLOWED_ACTIONS, ControlPlane


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, default=str))


def _plane(args: argparse.Namespace) -> ControlPlane:
    return ControlPlane(Path(args.brain_dir))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m brain.control.cli",
        description="Operator view over Project Brain's engineering subsystems",
    )
    parser.add_argument("--brain-dir", default=".brain", help="Brain state directory")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("summary", help="Combined operator summary")

    queue = sub.add_parser("queue", help="Human action queue")
    queue.add_argument("--repository-id", default="")
    queue.add_argument("--action-type", default="", choices=["", *sorted(ALLOWED_ACTIONS)])
    queue.add_argument("--max-priority", type=int, default=0, help="1=critical … 4=low")

    sub.add_parser("budgets", help="Configured budgets and observed usage")

    check = sub.add_parser("check-budget", help="Fail-closed admission check")
    check.add_argument("budget")
    check.add_argument("--requested", type=int, default=1)

    sub.add_parser("metrics", help="Health and observability metrics")
    sub.add_parser("health", help="Aggregate health verdict")

    write = sub.add_parser("write-budgets", help="Write the default budget policy file")
    write.add_argument("--force", action="store_true")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "summary":
        _print_json(_plane(args).summary())
        return 0

    if args.command == "queue":
        items = _plane(args).action_queue(repository_id=args.repository_id)
        if args.action_type:
            items = [i for i in items if i["action_type"] == args.action_type]
        if args.max_priority:
            items = [i for i in items if i["priority"] <= args.max_priority]
        _print_json({"items": items, "total": len(items)})
        return 0

    if args.command == "budgets":
        _print_json(_plane(args).budgets())
        return 0

    if args.command == "check-budget":
        try:
            usage = _plane(args).check_budget(args.budget, args.requested)
        except KeyError as exc:
            _print_json({"allowed": False, "error": str(exc)})
            return 1
        except (BudgetExceededError, BudgetUnknownError) as exc:
            _print_json({"allowed": False, "budget": args.budget, "error": str(exc)})
            return 1
        _print_json({"allowed": True, **usage.to_dict()})
        return 0

    if args.command == "metrics":
        _print_json(_plane(args).metrics())
        return 0

    if args.command == "health":
        health = _plane(args).health()
        _print_json(health)
        return 0 if health["status"] == "ok" else 1

    if args.command == "write-budgets":
        path = Path(args.brain_dir) / "control" / BUDGET_FILE_NAME
        if path.is_file() and not args.force:
            _print_json({"written": False, "path": str(path), "error": "already exists"})
            return 1
        BudgetPolicy().save(path)
        _print_json({"written": True, "path": str(path)})
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
