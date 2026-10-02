"""Repository Registry CLI.

Phase A6 — Command-line interface for repository registration,
inspection, and capability management.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from brain.lab.laboratory import BUILTIN_PROFILES
from brain.workspace.capabilities import CapabilitiesManager
from brain.workspace.models import (
    RepositoryRecord,
    TrustLevel,
    _generate_repository_id,
)
from brain.workspace.path_sandbox import PathSandbox, PathValidationError
from brain.workspace.registry_store import RegistryStore


def _get_store(brain_dir: Optional[str] = None) -> RegistryStore:
    if brain_dir:
        return RegistryStore(Path(brain_dir))
    return RegistryStore(Path(".brain/workspace"))


def _print_json(data):
    print(json.dumps(data, indent=2, default=str))


def cmd_register(args):
    """Register a repository."""
    store = _get_store(args.brain_dir)

    # Validate path
    existing = store.list_all()
    registered_canonicals = {
        PathSandbox.get_canonical_string(Path(r.canonical_root))
        for r in existing
        if not r.disabled
    }

    allowed_roots = [Path(args.path).resolve().parent, Path.cwd().resolve()]
    if args.allowed_root:
        for r in args.allowed_root:
            allowed_roots.append(Path(r).resolve())

    sandbox = PathSandbox(
        allowed_roots,
        allow_home=args.allow_home,
        registered_canonicals=registered_canonicals,
    )

    trust = TrustLevel(args.trust) if args.trust else TrustLevel.TRUSTED_INTERNAL
    require_git = trust != TrustLevel.FIXTURE

    try:
        resolved = sandbox.validate(args.path, require_git=require_git)
    except PathValidationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    canonical = str(resolved)
    normalized = PathSandbox.get_normalized_identity(resolved)
    repo_id = _generate_repository_id(canonical)

    # Detect defaults
    revision = PathSandbox.get_git_revision(resolved) if require_git else "none"
    default_branch = PathSandbox.get_git_default_branch(resolved) if require_git else "main"
    languages = PathSandbox.detect_languages(resolved)
    capabilities = CapabilitiesManager.get_defaults(trust)

    record = RepositoryRecord(
        repository_id=repo_id,
        display_name=args.name or resolved.name,
        canonical_root=canonical,
        normalized_root_identity=normalized,
        repository_type=("fixture" if trust == TrustLevel.FIXTURE else "git"),
        default_branch=default_branch,
        current_revision=revision,
        trust_level=trust.value,
        organization=args.org or "",
        owner=args.owner or "",
        languages=languages,
        capabilities=capabilities,
        # Both built-in profiles: the laboratory refuses any profile outside
        # this list, and `brain.lab.cli validate` defaults to "python-compile".
        allowed_validation_profiles=sorted(BUILTIN_PROFILES),
    )

    try:
        saved = store.register(record, actor="cli", source="cli", reason="manual registration")
        print(f"Registered: {saved.repository_id} ({saved.display_name})")
        _print_json(saved.to_dict())
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


def cmd_list(args):
    """List registered repositories."""
    store = _get_store(args.brain_dir)
    repos = store.list_all()

    if args.format == "json":
        _print_json([r.to_dict() for r in repos])
    else:
        for r in repos:
            status = "disabled" if r.disabled else r.trust_level
            print(f"  {r.repository_id}  {r.display_name:<30s}  [{status}]  rev={r.current_revision[:8] if r.current_revision != 'none' else 'none'}")
        print(f"\nTotal: {len(repos)} repositories")


def cmd_show(args):
    """Show repository details."""
    store = _get_store(args.brain_dir)
    record = store.get(args.repository_id)
    if not record:
        print(f"ERROR: Repository '{args.repository_id}' not found", file=sys.stderr)
        sys.exit(1)
    _print_json(record.to_dict())


def cmd_disable(args):
    """Disable a repository."""
    store = _get_store(args.brain_dir)
    try:
        record = store.disable(args.repository_id, actor="cli", source="cli", reason=args.reason or "manual disable")
        print(f"Disabled: {record.repository_id}")
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


def cmd_capabilities(args):
    """Show repository capabilities."""
    store = _get_store(args.brain_dir)
    record = store.get(args.repository_id)
    if not record:
        print(f"ERROR: Repository '{args.repository_id}' not found", file=sys.stderr)
        sys.exit(1)

    trust = TrustLevel(record.trust_level)
    issues = CapabilitiesManager.validate_capabilities(record.capabilities, trust)

    result = {
        "repository_id": record.repository_id,
        "trust_level": record.trust_level,
        "capabilities": record.capabilities,
        "validation_issues": issues,
    }
    _print_json(result)


def cmd_events(args):
    """Show registry audit events."""
    store = _get_store(args.brain_dir)
    events = store.list_events(repository_id=args.repository_id, limit=args.limit or 50)
    _print_json([e.to_dict() for e in events])


def cmd_verify(args):
    """Verify registry integrity."""
    store = _get_store(args.brain_dir)
    ok, msg = store.verify_integrity()
    print(f"{'OK' if ok else 'FAIL'}: {msg}")
    sys.exit(0 if ok else 1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="brain.workspace.cli", description="Repository Registry CLI")
    parser.add_argument("--brain-dir", dest="brain_dir", help="Brain storage directory")

    sub = parser.add_subparsers(dest="command")

    # repositories subcommand group
    repos_parser = sub.add_parser("repositories", help="Repository management")
    repos_sub = repos_parser.add_subparsers(dest="repos_command")

    # register
    reg = repos_sub.add_parser("register", help="Register a repository")
    reg.add_argument("--name", help="Display name")
    reg.add_argument("--path", required=True, help="Repository path")
    reg.add_argument("--trust", default="trusted_internal", help="Trust level")
    reg.add_argument("--org", help="Organization")
    reg.add_argument("--owner", help="Owner")
    reg.add_argument("--allowed-root", action="append", help="Additional allowed root paths")
    reg.add_argument("--allow-home", action="store_true", help="Allow home directory")
    reg.add_argument("--format", default="text")
    reg.set_defaults(func=cmd_register)

    # list
    lst = repos_sub.add_parser("list", help="List repositories")
    lst.add_argument("--format", default="text", choices=["text", "json"])
    lst.set_defaults(func=cmd_list)

    # show
    shw = repos_sub.add_parser("show", help="Show repository details")
    shw.add_argument("repository_id", help="Repository ID")
    shw.set_defaults(func=cmd_show)

    # disable
    dis = repos_sub.add_parser("disable", help="Disable a repository")
    dis.add_argument("repository_id", help="Repository ID")
    dis.add_argument("--reason", help="Reason for disabling")
    dis.set_defaults(func=cmd_disable)

    # capabilities
    cap = repos_sub.add_parser("capabilities", help="Show capabilities")
    cap.add_argument("repository_id", help="Repository ID")
    cap.set_defaults(func=cmd_capabilities)

    # events
    evt = repos_sub.add_parser("events", help="Show registry events")
    evt.add_argument("--repository-id", dest="repository_id", help="Filter by repository")
    evt.add_argument("--limit", type=int, default=50)
    evt.set_defaults(func=cmd_events)

    # verify
    ver = repos_sub.add_parser("verify", help="Verify registry integrity")
    ver.set_defaults(func=cmd_verify)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
