"""Change Laboratory CLI — Phase D9.

    python3 -m brain.lab.cli profiles
    python3 -m brain.lab.cli workspaces
    python3 -m brain.lab.cli validate --repository <id> --patch-file <path> [--profile <name>]
    python3 -m brain.lab.cli clean --workspace <id>
    python3 -m brain.lab.cli recover
    python3 -m brain.lab.cli session --session <id>

Repository roots come from the workspace registry; the caller never supplies a
filesystem path. Project Brain applies candidate patches only inside disposable
managed workspaces for validation — never to the authoritative checkout.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Optional

from brain.lab.laboratory import ChangeLaboratory
from brain.lab.models import PatchRecord, PatchSource
from brain.workspace.models import Capability
from brain.workspace.registry_store import RegistryStore


def _print_json(data) -> None:
    print(json.dumps(data, indent=2, default=str, sort_keys=True))


def _laboratory(brain_dir: Optional[str]) -> ChangeLaboratory:
    return ChangeLaboratory(Path(brain_dir) if brain_dir else Path(".brain"))


def _registry(registry_dir: Optional[str]) -> RegistryStore:
    return RegistryStore(Path(registry_dir) if registry_dir else Path(".brain/workspace"))


def _resolve_repository(args, required_capability: Capability):
    record = _registry(args.registry_dir).get(args.repository)
    if record is None:
        print(
            f"ERROR: Repository '{args.repository}' is not registered. "
            "Register it first with `python3 -m brain.workspace.cli repositories register`.",
            file=sys.stderr,
        )
        sys.exit(1)
    if record.disabled:
        print(f"ERROR: Repository '{args.repository}' is disabled", file=sys.stderr)
        sys.exit(1)
    if required_capability.value not in record.capabilities:
        print(
            f"ERROR: Repository '{record.repository_id}' lacks capability "
            f"'{required_capability.value}'",
            file=sys.stderr,
        )
        sys.exit(1)
    return record


def cmd_profiles(args) -> None:
    _print_json({"profiles": _laboratory(args.brain_dir).list_profiles()})


def cmd_workspaces(args) -> None:
    lab = _laboratory(args.brain_dir)
    _print_json(
        {
            "managed_root": str(lab.manager.managed_root),
            "workspaces": [w.to_dict() for w in lab.manager.list_workspaces()],
        }
    )


def cmd_validate(args) -> None:
    record = _resolve_repository(args, Capability.EXECUTE_VALIDATION)
    if Capability.APPLY_CANDIDATE_PATCHES.value not in record.capabilities:
        print(
            f"ERROR: Repository '{record.repository_id}' lacks capability "
            f"'{Capability.APPLY_CANDIDATE_PATCHES.value}'",
            file=sys.stderr,
        )
        sys.exit(1)

    patch_path = Path(args.patch_file)
    if not patch_path.is_file():
        print(f"ERROR: Patch file not found: {patch_path}", file=sys.stderr)
        sys.exit(1)

    lab = _laboratory(args.brain_dir)
    patch = PatchRecord(
        patch_id=args.patch_id or f"patch-{uuid.uuid4().hex[:10]}",
        source_type=PatchSource.UNIFIED_DIFF.value,
        repository_id=record.repository_id,
        base_revision=args.base or record.current_revision,
        patch_content=patch_path.read_text(encoding="utf-8"),
        allowed_paths=list(args.allow_path or []),
    )

    session = lab.run_session(
        repository_id=record.repository_id,
        repo_path=Path(record.canonical_root),
        base_revision=args.base or record.current_revision,
        patch=patch,
        profile_name=args.profile,
        cleanup=not args.keep_workspace,
        allowed_profiles=record.allowed_validation_profiles or None,
    )
    _print_json(session)
    if session["outcome"] != "passed":
        sys.exit(1)


def cmd_clean(args) -> None:
    lab = _laboratory(args.brain_dir)
    report = lab.manager.clean_workspace(args.workspace, reason=args.reason or "cli cleanup")
    _print_json(report.to_dict())
    if not report.removed:
        sys.exit(1)


def cmd_recover(args) -> None:
    lab = _laboratory(args.brain_dir)
    expired = lab.manager.expire_due()
    report = lab.manager.recover_orphans()
    payload = report.to_dict()
    payload["expired_workspaces"] = expired
    _print_json(payload)


def cmd_session(args) -> None:
    session = _laboratory(args.brain_dir).get_session(args.session)
    if session is None:
        print(f"ERROR: Unknown session '{args.session}'", file=sys.stderr)
        sys.exit(1)
    _print_json(session)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m brain.lab.cli",
        description="Change Laboratory: disposable workspaces for candidate patch validation",
    )
    parser.add_argument("--brain-dir", help="Brain state directory (default: .brain)")
    parser.add_argument(
        "--registry-dir", help="Repository registry directory (default: .brain/workspace)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("profiles", help="List available validation profiles").set_defaults(
        func=cmd_profiles
    )
    sub.add_parser("workspaces", help="List managed workspaces").set_defaults(
        func=cmd_workspaces
    )

    validate = sub.add_parser(
        "validate", help="Validate a candidate patch inside a disposable workspace"
    )
    validate.add_argument("--repository", required=True, help="Registered repository id")
    validate.add_argument("--patch-file", required=True, help="Unified diff to validate")
    validate.add_argument("--patch-id", help="Explicit patch identifier")
    validate.add_argument("--base", help="Base revision (default: registry revision)")
    validate.add_argument("--profile", default="python-compile", help="Validation profile name")
    validate.add_argument(
        "--allow-path",
        action="append",
        help="Restrict the patch to this path prefix (repeatable)",
    )
    validate.add_argument(
        "--keep-workspace",
        action="store_true",
        help="Keep the workspace after validation for manual inspection",
    )
    validate.set_defaults(func=cmd_validate)

    clean = sub.add_parser("clean", help="Dispose of one managed workspace")
    clean.add_argument("--workspace", required=True)
    clean.add_argument("--reason", help="Recorded cleanup reason")
    clean.set_defaults(func=cmd_clean)

    recover = sub.add_parser(
        "recover", help="Expire due workspaces and reconcile orphan directories"
    )
    recover.set_defaults(func=cmd_recover)

    session = sub.add_parser("session", help="Show a stored laboratory session")
    session.add_argument("--session", required=True)
    session.set_defaults(func=cmd_session)

    return parser


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
