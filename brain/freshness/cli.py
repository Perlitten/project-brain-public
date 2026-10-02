"""Freshness and Incremental Intelligence CLI — Phase C6.

    python3 -m brain.freshness.cli status --repository <id>
    python3 -m brain.freshness.cli explain --artifact <artifact-id>
    python3 -m brain.freshness.cli plan --repository <id> --base <rev> --candidate <rev>
    python3 -m brain.freshness.cli rebuild --repository <id>

``rebuild`` is an operational action: it derives and activates a new graph
generation for a repository the operator explicitly names. It never runs
implicitly from a status query.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Tuple

from brain.freshness.generation_deriver import GenerationDeriver
from brain.freshness.incremental_planner import IncrementalPlanner
from brain.freshness.models import ArtifactType, FreshnessState
from brain.freshness.tracker import FreshnessTracker
from brain.graph.generation_manager import GraphGenerationManager
from brain.workspace.registry_store import RegistryStore


def _print_json(data) -> None:
    print(json.dumps(data, indent=2, default=str, sort_keys=True))


def _registry(brain_dir: Optional[str]) -> RegistryStore:
    return RegistryStore(Path(brain_dir) if brain_dir else Path(".brain/workspace"))


def _tracker(brain_dir: Optional[str]) -> FreshnessTracker:
    return FreshnessTracker(Path(brain_dir) if brain_dir else Path(".brain/freshness"))


def _resolve_repository(args) -> Tuple[str, Path]:
    """Resolve a registered repository to (repository_id, canonical root)."""
    store = _registry(args.registry_dir)
    record = store.get(args.repository)
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
    return record.repository_id, Path(record.canonical_root)


def cmd_status(args) -> None:
    repo_id, repo_path = _resolve_repository(args)
    tracker = _tracker(args.brain_dir)
    mgr = GraphGenerationManager(repo_path / ".brain")

    observed = FreshnessTracker.resolve_revision(repo_path)
    active_gen = mgr.get_active_generation_id()
    generations = mgr.list_generations()
    active_meta = next((g for g in generations if g.generation_id == active_gen), None)

    records = tracker.list_by_repository(repo_id)
    graph_state = FreshnessState.UNKNOWN.value
    if active_meta and observed:
        graph_state = (
            FreshnessState.CURRENT.value
            if active_meta.git_revision == observed
            else FreshnessState.STALE.value
        )

    _print_json(
        {
            "repository_id": repo_id,
            "observed_revision": observed or None,
            "active_generation_id": active_gen,
            "active_generation_revision": active_meta.git_revision if active_meta else None,
            "graph_freshness": graph_state,
            "generation_count": len(generations),
            "artifacts": [r.to_dict() for r in records],
        }
    )


def cmd_explain(args) -> None:
    tracker = _tracker(args.brain_dir)
    result = tracker.explain(args.artifact)
    _print_json(result)
    if not result.get("found"):
        sys.exit(1)


def cmd_plan(args) -> None:
    repo_id, repo_path = _resolve_repository(args)
    mgr = GraphGenerationManager(repo_path / ".brain")
    base_store = mgr.get_active_graph_store()

    plan = IncrementalPlanner().plan_update(
        repo_path=repo_path,
        base_revision=args.base,
        candidate_revision=args.candidate,
        repository_id=repo_id,
        base_store=base_store,
    )
    _print_json(
        {
            "plan": plan.to_dict(),
            "preview": IncrementalPlanner.derivation_preview(plan),
        }
    )


def cmd_rebuild(args) -> None:
    repo_id, repo_path = _resolve_repository(args)
    tracker = _tracker(args.brain_dir)
    mgr = GraphGenerationManager(repo_path / ".brain")

    active_gen = mgr.get_active_generation_id()
    base_meta = next(
        (g for g in mgr.list_generations() if g.generation_id == active_gen), None
    )
    base_revision = args.base or (base_meta.git_revision if base_meta else "")
    candidate = args.candidate or FreshnessTracker.resolve_revision(repo_path) or "HEAD"

    artifact_id = f"graph:{repo_id}"
    tracker.mark_building(artifact_id, caused_by="cli rebuild")

    planner = IncrementalPlanner()
    if base_revision and active_gen:
        plan = planner.plan_update(
            repo_path=repo_path,
            base_revision=base_revision,
            candidate_revision=candidate,
            repository_id=repo_id,
            base_store=mgr.get_active_graph_store(),
        )
    else:
        plan = planner.plan_update(
            repo_path=repo_path,
            base_revision=base_revision or candidate,
            candidate_revision=candidate,
            repository_id=repo_id,
        )
        plan.fallback_reason = plan.fallback_reason or "No active generation to derive from"
        plan.decision = "full_rebuild"

    if args.full:
        plan.decision = "full_rebuild"
        plan.fallback_reason = "operator requested full rebuild"

    deriver = GenerationDeriver(repo_path, repository_id=repo_id)
    report = deriver.derive(plan, base_generation_id=active_gen, activate=not args.no_activate)

    if report.validation_errors:
        tracker.mark_failed(artifact_id, "; ".join(report.validation_errors)[:400])
    else:
        tracker.record(
            artifact_id=artifact_id,
            artifact_type=ArtifactType.REPOSITORY_GRAPH.value,
            repository_id=repo_id,
            state=FreshnessState.CURRENT,
            source_revision=report.candidate_revision,
            observed_revision=FreshnessTracker.resolve_revision(repo_path),
            evidence=f"generation {report.new_generation_id} ({report.decision})",
        )

    _print_json({"plan": plan.to_dict(), "derivation": report.to_dict()})
    if report.validation_errors:
        sys.exit(1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="brain.freshness.cli", description="Freshness and incremental intelligence CLI"
    )
    parser.add_argument("--brain-dir", dest="brain_dir", help="Freshness storage directory")
    parser.add_argument("--registry-dir", dest="registry_dir", help="Registry storage directory")

    sub = parser.add_subparsers(dest="command")

    status = sub.add_parser("status", help="Show freshness status for a repository")
    status.add_argument("--repository", required=True, help="Registered repository ID")
    status.set_defaults(func=cmd_status)

    explain = sub.add_parser("explain", help="Explain one artifact's freshness")
    explain.add_argument("--artifact", required=True, help="Artifact ID")
    explain.set_defaults(func=cmd_explain)

    plan = sub.add_parser("plan", help="Plan an incremental update without building")
    plan.add_argument("--repository", required=True)
    plan.add_argument("--base", required=True, help="Base revision")
    plan.add_argument("--candidate", required=True, help="Candidate revision")
    plan.set_defaults(func=cmd_plan)

    rebuild = sub.add_parser("rebuild", help="Derive and activate a new graph generation")
    rebuild.add_argument("--repository", required=True)
    rebuild.add_argument("--base", help="Base revision (defaults to active generation)")
    rebuild.add_argument("--candidate", help="Candidate revision (defaults to HEAD)")
    rebuild.add_argument("--full", action="store_true", help="Force a full rebuild")
    rebuild.add_argument(
        "--no-activate", action="store_true", help="Build but leave the generation inactive"
    )
    rebuild.set_defaults(func=cmd_rebuild)

    return parser


def main(argv=None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()
