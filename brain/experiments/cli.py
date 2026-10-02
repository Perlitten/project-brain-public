"""Remediation experiments CLI — Phase E7.

    python3 -m brain.experiments.cli create --repository <id> --option <file> [...]
    python3 -m brain.experiments.cli run <experiment-id>
    python3 -m brain.experiments.cli show <experiment-id>
    python3 -m brain.experiments.cli compare <experiment-id>
    python3 -m brain.experiments.cli cancel <experiment-id>
    python3 -m brain.experiments.cli recommend <experiment-id>
    python3 -m brain.experiments.cli action <experiment-id> --action accept_for_export
    python3 -m brain.experiments.cli export <experiment-id> --option <id> --out <dir>
    python3 -m brain.experiments.cli package <experiment-id>

Repository roots come from the workspace registry. No subcommand writes to an
authoritative repository: `export` produces a patch file for a human to apply.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, NoReturn, Optional

from brain.experiments.models import (
    Experiment,
    ExperimentOption,
    ExportNotAuthorizedError,
    ExportStaleError,
    HumanAction,
)
from brain.experiments.orchestrator import ExperimentManager, ExperimentStore
from brain.experiments.packaging import ExperimentPackager
from brain.lab.laboratory import ChangeLaboratory
from brain.workspace.models import Capability
from brain.workspace.registry_store import RegistryStore


def _print_json(data) -> None:
    print(json.dumps(data, indent=2, default=str, sort_keys=True))


def _fail(message: str) -> NoReturn:
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def _brain_dir(args) -> Path:
    return Path(args.brain_dir) if args.brain_dir else Path(".brain")


def _manager(args) -> ExperimentManager:
    brain_dir = _brain_dir(args)
    return ExperimentManager(
        ExperimentStore(brain_dir / "experiments"), ChangeLaboratory(brain_dir)
    )


def _registry(args) -> RegistryStore:
    return RegistryStore(
        Path(args.registry_dir) if args.registry_dir else Path(".brain/workspace")
    )


def _require_repository(args, repository_id: str, *capabilities: Capability):
    record = _registry(args).get(repository_id)
    if record is None:
        _fail(f"Repository '{repository_id}' is not registered")
    if record.disabled:
        _fail(f"Repository '{repository_id}' is disabled")
    for capability in capabilities:
        if capability.value not in record.capabilities:
            _fail(
                f"Repository '{record.repository_id}' lacks capability "
                f"'{capability.value}'"
            )
    return record


def _load_options(paths: List[str]) -> List[ExperimentOption]:
    options: List[ExperimentOption] = []
    for index, raw in enumerate(paths, start=1):
        path = Path(raw)
        if not path.is_file():
            _fail(f"Option file not found: {path}")
        if path.suffix == ".json":
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                _fail(f"Option file {path} is not valid JSON: {exc}")
            data.setdefault("option_id", f"opt-{index}")
            options.append(ExperimentOption.from_dict(data))
        else:
            options.append(
                ExperimentOption(
                    option_id=f"opt-{index}",
                    candidate_patch=path.read_text(encoding="utf-8"),
                    patch_provenance=f"file:{path.name}",
                )
            )
    return options


def _require_experiment(manager: ExperimentManager, experiment_id: str) -> Experiment:
    experiment = manager.get(experiment_id)
    if experiment is None:
        _fail(f"Unknown experiment '{experiment_id}'")
    return experiment


# ── subcommands ──


def cmd_create(args) -> None:
    record = _require_repository(
        args,
        args.repository,
        Capability.CREATE_WORKTREES,
        Capability.APPLY_CANDIDATE_PATCHES,
        Capability.EXECUTE_VALIDATION,
    )
    options = _load_options(args.option or [])
    if not options:
        _fail("At least one --option is required")

    experiment = _manager(args).create_experiment(
        repository_id=record.repository_id,
        base_revision=args.base or record.current_revision,
        options=options,
        source_plan_id=args.plan or "",
        source_finding_id=args.finding or "",
        graph_generation=args.generation or "",
        policy_version=args.policy_version or "",
        creation_actor=args.actor,
        validation_profile=args.profile,
        max_parallel_options=args.max_parallel,
        retain_workspaces=args.retain_workspaces,
    )
    _print_json(experiment.to_dict())


def cmd_run(args) -> None:
    manager = _manager(args)
    experiment = _require_experiment(manager, args.experiment_id)
    record = _require_repository(
        args,
        experiment.repository_id,
        Capability.CREATE_WORKTREES,
        Capability.APPLY_CANDIDATE_PATCHES,
        Capability.EXECUTE_VALIDATION,
    )
    updated = manager.run_experiment(
        experiment.experiment_id,
        Path(record.canonical_root),
        authoritative_revision=record.current_revision,
        allowed_profiles=record.allowed_validation_profiles or None,
    )
    if updated is None:
        _fail(f"Experiment '{experiment.experiment_id}' no longer exists")
    _print_json(updated.to_dict())


def cmd_show(args) -> None:
    _print_json(_require_experiment(_manager(args), args.experiment_id).to_dict())


def cmd_list(args) -> None:
    _print_json(
        {
            "experiments": [
                {
                    "experiment_id": e.experiment_id,
                    "repository_id": e.repository_id,
                    "state": e.state,
                    "conclusion": e.conclusion,
                    "recommended_option_id": e.recommended_option_id,
                    "created_at_utc": e.created_at_utc,
                }
                for e in _manager(args).list_all()
            ]
        }
    )


def cmd_compare(args) -> None:
    manager = _manager(args)
    _require_experiment(manager, args.experiment_id)
    result = manager.complete_comparison(args.experiment_id)
    if result is None:
        _fail(f"Experiment '{args.experiment_id}' no longer exists")
    _print_json(result.to_dict())


def cmd_cancel(args) -> None:
    manager = _manager(args)
    _require_experiment(manager, args.experiment_id)
    cancelled = manager.request_cancel(args.experiment_id)
    if cancelled is None:
        _fail(f"Experiment '{args.experiment_id}' no longer exists")
    _print_json(cancelled.to_dict())


def cmd_recommend(args) -> None:
    manager = _manager(args)
    _require_experiment(manager, args.experiment_id)
    _print_json(manager.get_recommendation(args.experiment_id))


def cmd_action(args) -> None:
    manager = _manager(args)
    _require_experiment(manager, args.experiment_id)
    try:
        experiment = manager.record_human_action(
            args.experiment_id,
            action=args.action,
            actor=args.actor,
            option_id=args.option_id or "",
            note=args.note or "",
        )
        if experiment is None:
            _fail(f"Experiment '{args.experiment_id}' no longer exists")
    except ValueError as exc:
        _fail(str(exc))
    _print_json(
        {
            "experiment_id": experiment.experiment_id,
            "human_action": experiment.human_action,
            "human_actions": [a.to_dict() for a in experiment.human_actions],
            "note": "Recording a human decision does not apply the patch.",
        }
    )


def cmd_export(args) -> None:
    manager = _manager(args)
    experiment = _require_experiment(manager, args.experiment_id)
    record = _require_repository(args, experiment.repository_id, Capability.EXPORT_PATCH)
    try:
        export = manager.export_patch(
            args.experiment_id,
            args.option_id,
            authoritative_revision=args.authoritative_revision or record.current_revision,
            revalidated=args.revalidated,
        )
    except (ExportStaleError, ExportNotAuthorizedError, LookupError) as exc:
        _fail(str(exc))

    if args.out:
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{export.export_id}.diff").write_text(
            export.unified_diff, encoding="utf-8"
        )
        (out_dir / f"{export.export_id}.json").write_text(
            json.dumps(export.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
        )
    _print_json(export.to_dict())


def cmd_package(args) -> None:
    manager = _manager(args)
    experiment = _require_experiment(manager, args.experiment_id)
    packager = ExperimentPackager(
        Path(args.reports_dir) if args.reports_dir else Path("reports/change-experiments")
    )
    manifest = packager.build(
        experiment,
        manager.get_comparison(args.experiment_id),
        manager.get_recommendation(args.experiment_id),
    )
    _print_json(
        {
            "package_dir": str(packager.package_dir(experiment.experiment_id)),
            "manifest": manifest,
        }
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m brain.experiments.cli",
        description="Remediation experiments: compare candidate patches in isolated workspaces",
    )
    parser.add_argument("--brain-dir", help="Brain state directory (default: .brain)")
    parser.add_argument(
        "--registry-dir", help="Repository registry directory (default: .brain/workspace)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="Create an experiment from candidate patches")
    create.add_argument("--repository", required=True)
    create.add_argument(
        "--option",
        action="append",
        required=True,
        help="Candidate patch (.diff) or option descriptor (.json); repeatable",
    )
    create.add_argument("--plan", help="Source remediation plan id")
    create.add_argument("--finding", help="Source finding id")
    create.add_argument("--base", help="Base revision (default: registry revision)")
    create.add_argument("--generation", help="Graph generation id")
    create.add_argument("--policy-version", help="Architecture policy version")
    create.add_argument("--profile", default="python-standard")
    create.add_argument("--actor", default="cli")
    create.add_argument("--max-parallel", type=int, default=1)
    create.add_argument("--retain-workspaces", action="store_true")
    create.set_defaults(func=cmd_create)

    for name, handler, help_text in (
        ("run", cmd_run, "Execute every pending option"),
        ("show", cmd_show, "Show one experiment"),
        ("compare", cmd_compare, "Re-run the deterministic comparison"),
        ("cancel", cmd_cancel, "Request cancellation"),
        ("recommend", cmd_recommend, "Show the recommendation"),
    ):
        node = sub.add_parser(name, help=help_text)
        node.add_argument("experiment_id")
        node.set_defaults(func=handler)

    sub.add_parser("list", help="List experiments").set_defaults(func=cmd_list)

    action = sub.add_parser("action", help="Record a human workflow decision")
    action.add_argument("experiment_id")
    action.add_argument(
        "--action", required=True, choices=sorted(a.value for a in HumanAction)
    )
    action.add_argument("--actor", default="cli")
    action.add_argument("--option-id")
    action.add_argument("--note")
    action.set_defaults(func=cmd_action)

    export = sub.add_parser("export", help="Export an accepted candidate patch for a human")
    export.add_argument("experiment_id")
    export.add_argument("--option-id", required=True)
    export.add_argument("--authoritative-revision")
    export.add_argument(
        "--revalidated",
        action="store_true",
        help="Explicit revalidation was performed against the new revision",
    )
    export.add_argument("--out", help="Directory to write the patch package into")
    export.set_defaults(func=cmd_export)

    package = sub.add_parser("package", help="Write the experiment evidence package")
    package.add_argument("experiment_id")
    package.add_argument("--reports-dir")
    package.set_defaults(func=cmd_package)

    return parser


def main(argv: Optional[List[str]] = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
