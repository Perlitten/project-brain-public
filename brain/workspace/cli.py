"""Repository Registry CLI.

Phase A6 — Command-line interface for repository registration,
inspection, and capability management.

Solo state reads and writes the local registry file (``--brain-dir``, default
``.brain/workspace``). In team state every command uses the shared registry
table the API and the worker read (``brain.team.registry``): the local file is
imported into it once while the table is empty, ``register`` needs
``--project`` and links the repository to that team project in the same
transaction, and every change is recorded in the team activity feed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import NoReturn, Optional

from brain.lab.laboratory import BUILTIN_PROFILES
from brain.workspace.capabilities import CapabilitiesManager
from brain.workspace.models import (
    RepositoryRecord,
    TrustLevel,
    _generate_repository_id,
)
from brain.workspace.path_sandbox import PathSandbox, PathValidationError
from brain.workspace.registry_store import RegistryStore


def _registry_dir(brain_dir: Optional[str] = None) -> Path:
    return Path(brain_dir) if brain_dir else Path(".brain/workspace")


# Team-state reads hand the ``cmd_*`` readers the shared registry in place of the file store.
_team_snapshot: Optional["_Snapshot"] = None


def _get_store(brain_dir: Optional[str] = None):
    if _team_snapshot is not None:
        return _team_snapshot
    return RegistryStore(_registry_dir(brain_dir))


def _print_json(data):
    print(json.dumps(data, indent=2, default=str))


def _fail(message: str, code: int = 1) -> NoReturn:
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(code)


def cmd_register(args):
    """Register a repository."""
    store = _get_store(args.brain_dir)
    record = _build_record(args, store.list_all())
    try:
        saved = store.register(record, actor="cli", source="cli", reason="manual registration")
        print(f"Registered: {saved.repository_id} ({saved.display_name})")
        _print_json(saved.to_dict())
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


def _build_record(args, existing: list[RepositoryRecord]) -> RepositoryRecord:
    """Validate ``args.path`` against the registered roots and build its record; exits on an invalid path."""
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
    return record


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


def cmd_enable(args):
    """Re-enable a disabled repository at --trust, or at the trust level it had before it was disabled."""
    from brain.team.registry import RegistryConflict, is_disabled, restored_trust_level

    store = _get_store(args.brain_dir)
    record = store.get(args.repository_id)
    if record is None:
        _fail(f"Repository '{args.repository_id}' not found")
    if not is_disabled(record):
        _fail(f"Repository '{args.repository_id}' is not disabled")
    try:
        level = restored_trust_level(store.list_events(repository_id=args.repository_id, limit=1_000_000), args.trust)
        record = store.update(args.repository_id, {"disabled": False, "trust_level": level}, record.version,
                              actor="cli", source="cli", reason=args.reason or "manual enable")
    except (ValueError, RegistryConflict) as exc:
        _fail(str(exc))
    print(f"Enabled: {record.repository_id} ({record.trust_level})")


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
    reg.add_argument("--project", help="Team state only (required there): the team project id or name "
                                       "whose members may use the repository")
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

    # enable
    enb = repos_sub.add_parser("enable", help="Re-enable a disabled repository")
    enb.add_argument("repository_id", help="Repository ID")
    enb.add_argument("--trust", help="Trust level to restore (default: the level before it was disabled)")
    enb.add_argument("--reason", help="Reason for re-enabling")
    enb.set_defaults(func=cmd_enable)

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


# ── team state: the shared registry table ──


async def run_team(args) -> bool:
    """Run ``args`` against the shared registry table when the deployment is in team state.

    Returns ``False`` in solo state (the caller then uses the local file, as
    before). When the team database cannot be reached the CLI falls back to the
    local file with a warning, unless ``BRAIN_TEAM_MODE`` is on or the registry
    directory carries the team-state marker, where it refuses
    (``brain.team.cli_registry``, shared with the other offline CLIs)."""
    from brain.team.cli_registry import OfflineRegistryError, cli_team_state

    directory = _registry_dir(args.brain_dir)
    try:
        if not await cli_team_state(directory):
            return False
        from brain.database import session as db
        from brain.team import registry as shared

        try:
            await shared.ensure_legacy_imported(db.async_session_factory, directory)
            await _TEAM_COMMANDS[args.func](args, db.async_session_factory, directory)
        except Exception as exc:  # noqa: BLE001 - team state must never fall back to the local file
            raise OfflineRegistryError(
                f"Cannot read the shared workspace registry in team state ({type(exc).__name__}); "
                "refusing to use the local registry file") from exc
    except OfflineRegistryError as exc:
        _fail(str(exc), code=2)
    return True


async def _team_register(args, factory, directory: Path) -> None:
    from sqlalchemy import select

    from brain.database.models import Repository
    from brain.database.repository_utils import find_repository_id
    from brain.team import provisioning
    from brain.team import registry as shared
    from brain.team.authorization import TeamConflict

    if not args.project:
        _fail("In team state, registering a repository needs --project: the team project (id or name) whose "
              "members may use it. A registration linked to no project would be trusted but usable by no one.",
              code=2)
    try:
        async with factory() as session, session.begin():
            record = _build_record(args, (await shared.team_registry(session, directory)).list_all())
            repository_id = await find_repository_id(session, record.canonical_root)
            if repository_id is None:
                _fail("In team state, a repository must be indexed before it is registered, so it can be linked "
                      "to a team project: index it, then register it with --project")
            project = await provisioning.resolve_project(session, args.project)
            if project.disabled_at:
                _fail(f"Project '{project.name}' is disabled")
            repository = (await session.execute(select(Repository).where(Repository.id == repository_id))).scalar_one()
            await provisioning.attach_repository(session, project_id=project.id, repository=repository)
            saved = await shared.insert_entry(session, record, actor="cli", source="cli", reason="manual registration")
            await shared.audit(session, "registered", saved,
                               f"Registered repository {saved.display_name} ({saved.trust_level})",
                               actor_label="cli", project_id=project.id)
    except (ValueError, provisioning.TeamNotFound, TeamConflict) as exc:
        _fail(str(exc))
    print(f"Registered: {saved.repository_id} ({saved.display_name}) in project {project.name}")
    _print_json(saved.to_dict())


async def _team_change(args, factory, kind: str, change) -> None:
    from brain.team import registry as shared

    try:
        async with factory() as session, session.begin():
            record = await change(session)
            await shared.audit(session, kind, record,
                               f"{kind.capitalize()} repository {record.display_name} ({record.trust_level})",
                               actor_label="cli")
    except (ValueError, shared.RegistryConflict) as exc:
        _fail(str(exc))
    print(f"{kind.capitalize()}: {record.repository_id} ({record.trust_level})")


async def _team_disable(args, factory, _directory: Path) -> None:
    from brain.team import registry as shared

    await _team_change(args, factory, "disabled", lambda session: shared.disable_entry(
        session, args.repository_id, actor="cli", source="cli", reason=args.reason or "manual disable"))


async def _team_enable(args, factory, _directory: Path) -> None:
    from brain.team import registry as shared

    await _team_change(args, factory, "enabled", lambda session: shared.enable_entry(
        session, args.repository_id, trust_level=args.trust, actor="cli", source="cli",
        reason=args.reason or "manual enable"))


class _Snapshot:
    """The shared registry and its events, read once, in the shape the ``cmd_*`` readers use."""

    def __init__(self, registry, events):
        self._registry, self._events = registry, events

    def __getattr__(self, name):
        return getattr(self._registry, name)

    def list_events(self, repository_id=None, limit=50):
        return [event for event in self._events
                if repository_id is None or event.repository_id == repository_id][-limit:]


async def _team_read(args, factory, directory: Path) -> None:
    from brain.team import registry as shared

    async with factory() as session:
        registry = await shared.team_registry(session, directory)
        events = await shared.team_events(session, getattr(args, "repository_id", None),
                                          getattr(args, "limit", None) or 50, directory)
    global _team_snapshot
    _team_snapshot = _Snapshot(registry, events)
    try:
        args.func(args)
    finally:
        _team_snapshot = None


_TEAM_COMMANDS = {
    cmd_register: _team_register,
    cmd_disable: _team_disable,
    cmd_enable: _team_enable,
    cmd_list: _team_read,
    cmd_show: _team_read,
    cmd_capabilities: _team_read,
    cmd_events: _team_read,
    cmd_verify: _team_read,
}


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(1)

    if not asyncio.run(run_team(args)):
        args.func(args)


if __name__ == "__main__":
    main()
