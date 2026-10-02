"""Portfolio CLI — Phase B7.

Commands for portfolio management, graph building, and inspection.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from brain.portfolio.graph_builder import PortfolioCoupling, PortfolioGraphBuilder
from brain.portfolio.models import parse_portfolio_config
from brain.portfolio.store import PortfolioStore
from brain.workspace.registry_store import RegistryStore


def _get_store(brain_dir: Optional[str] = None) -> PortfolioStore:
    d = Path(brain_dir) if brain_dir else Path(".brain/portfolio")
    return PortfolioStore(d)


def _print_json(data):
    print(json.dumps(data, indent=2, default=str))


def cmd_list(args):
    store = _get_store(args.brain_dir)
    portfolios = store.list_portfolios()
    if args.format == "json":
        _print_json([p.to_dict() for p in portfolios])
    else:
        for p in portfolios:
            repos = ", ".join(r.alias for r in p.repositories)
            print(f"  {p.portfolio_id}  {p.display_name:<30s}  repos=[{repos}]")
        print(f"\nTotal: {len(portfolios)} portfolios")


def cmd_create(args):
    store = _get_store(args.brain_dir)
    config_path = Path(args.config)
    if not config_path.exists():
        print(f"ERROR: Config file not found: {args.config}", file=sys.stderr)
        sys.exit(1)

    import yaml  # lazy import — only needed for config parsing
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    try:
        portfolio = parse_portfolio_config(config)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    saved = store.save_portfolio(portfolio)
    print(f"Created portfolio: {saved.portfolio_id} ({saved.display_name})")
    _print_json(saved.to_dict())


def cmd_build(args):
    store = _get_store(args.brain_dir)
    portfolio = store.get_portfolio(args.portfolio_id)
    if not portfolio:
        print(f"ERROR: Portfolio '{args.portfolio_id}' not found", file=sys.stderr)
        sys.exit(1)

    # Revisions come from the registry, not from a placeholder: a generation
    # records which revision it was built from, and storing a literal "HEAD"
    # would make an unresolvable record look like a current one.
    # ".brain/workspace" is where every other entry point reads and writes the
    # registry; a different default here would make `build` report a correctly
    # registered portfolio as unregistered.
    registry = RegistryStore(Path(args.registry_dir) if args.registry_dir else Path(".brain/workspace"))
    revisions: dict[str, str] = {}
    generations: dict[str, str] = {}
    missing: list[str] = []
    for repo in portfolio.repositories:
        record = registry.get(repo.repository_id)
        if record is None:
            missing.append(repo.repository_id)
            continue
        revisions[repo.repository_id] = record.current_revision
        generations[repo.repository_id] = record.graph_generation_status
    if missing:
        print(
            "ERROR: not registered: " + ", ".join(missing) + "\n"
            "Register each portfolio repository first: brain.workspace.cli repositories register",
            file=sys.stderr,
        )
        sys.exit(1)

    builder = PortfolioGraphBuilder(portfolio)
    gen = builder.build_generation(revisions, generations)

    if gen.quality_report.get("passed", False):
        gen = builder.activate(gen)
        portfolio.active_generation_id = gen.generation_id
        store.save_portfolio(portfolio)

    store.save_generation(gen)
    print(f"Built generation: {gen.generation_id} (status={gen.status})")
    _print_json(gen.quality_report)


def cmd_summary(args):
    store = _get_store(args.brain_dir)
    portfolio = store.get_portfolio(args.portfolio_id)
    if not portfolio:
        print(f"ERROR: Portfolio '{args.portfolio_id}' not found", file=sys.stderr)
        sys.exit(1)

    _print_json(portfolio.to_dict())


def cmd_dependencies(args):
    store = _get_store(args.brain_dir)
    portfolio = store.get_portfolio(args.portfolio_id)
    if not portfolio:
        print(f"ERROR: Portfolio '{args.portfolio_id}' not found", file=sys.stderr)
        sys.exit(1)

    if not portfolio.active_generation_id:
        print("No active generation. Run 'build' first.", file=sys.stderr)
        sys.exit(1)

    gen = store.get_generation(portfolio.active_generation_id)
    if not gen:
        print("Active generation not found", file=sys.stderr)
        sys.exit(1)

    deps = [r.to_dict() for r in gen.relationships]
    _print_json({"total": len(deps), "dependencies": deps})


def cmd_cycles(args):
    store = _get_store(args.brain_dir)
    portfolio = store.get_portfolio(args.portfolio_id)
    if not portfolio:
        print(f"ERROR: Portfolio '{args.portfolio_id}' not found", file=sys.stderr)
        sys.exit(1)

    if not portfolio.active_generation_id:
        print("No active generation", file=sys.stderr)
        sys.exit(1)

    gen = store.get_generation(portfolio.active_generation_id)
    if not gen:
        print("Generation not found", file=sys.stderr)
        sys.exit(1)

    coupling = PortfolioCoupling.calculate(gen, portfolio)
    _print_json({"circular_dependencies": coupling["circular_dependencies"]})


def cmd_coupling(args):
    store = _get_store(args.brain_dir)
    portfolio = store.get_portfolio(args.portfolio_id)
    if not portfolio:
        print(f"ERROR: Portfolio '{args.portfolio_id}' not found", file=sys.stderr)
        sys.exit(1)

    if not portfolio.active_generation_id:
        print("No active generation", file=sys.stderr)
        sys.exit(1)

    gen = store.get_generation(portfolio.active_generation_id)
    if not gen:
        print("Generation not found", file=sys.stderr)
        sys.exit(1)

    coupling = PortfolioCoupling.calculate(gen, portfolio)
    _print_json(coupling)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="brain.portfolio.cli", description="Portfolio CLI")
    parser.add_argument("--brain-dir", dest="brain_dir", help="Portfolio storage directory")
    parser.add_argument("--registry-dir", dest="registry_dir", help="Registry storage directory")

    sub = parser.add_subparsers(dest="command")

    listing = sub.add_parser("list", help="List portfolios")
    listing.add_argument("--format", default="text", choices=["text", "json"])
    listing.set_defaults(func=cmd_list)

    create = sub.add_parser("create", help="Create portfolio from config")
    create.add_argument("--config", required=True)
    create.set_defaults(func=cmd_create)

    build = sub.add_parser("build", help="Build portfolio graph")
    build.add_argument("portfolio_id")
    build.set_defaults(func=cmd_build)

    summary = sub.add_parser("summary", help="Show portfolio summary")
    summary.add_argument("portfolio_id")
    summary.set_defaults(func=cmd_summary)

    deps = sub.add_parser("dependencies", help="Show dependencies")
    deps.add_argument("portfolio_id")
    deps.set_defaults(func=cmd_dependencies)

    cyc = sub.add_parser("cycles", help="Detect cycles")
    cyc.add_argument("portfolio_id")
    cyc.set_defaults(func=cmd_cycles)

    coup = sub.add_parser("coupling", help="Show coupling metrics")
    coup.add_argument("portfolio_id")
    coup.set_defaults(func=cmd_coupling)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func") or args.func is None:
        parser.print_help()
        sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()
