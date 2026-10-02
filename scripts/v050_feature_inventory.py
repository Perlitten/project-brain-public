"""RC Phase 1 — authoritative, code-derived feature inventory for v0.5.0.

Everything here is read out of the running code: subsystem modules are imported
and introspected, CLI parsers are built and walked, and the API surface comes
from the FastAPI application's own OpenAPI document. Nothing is transcribed by
hand, so the inventory cannot drift away from the implementation without the
regeneration failing or changing.

The inventory also reports two reference checks that are the basis for the
"remove or mark" decisions: which v0.5.0 modules nothing imports, and which
v0.5.0 endpoints nothing exercises.

    py -3 scripts/v050_feature_inventory.py [--out PATH]
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

GENERATION_COMMAND = "py -3 scripts/v050_feature_inventory.py"

# The eighteen v0.5.0 subsystems the release must account for, mapped to the
# packages that implement them.
SUBSYSTEMS: List[Dict[str, Any]] = [
    {"key": "workspace_registry", "title": "Workspace and repository registry",
     "modules": ["brain.workspace.models", "brain.workspace.registry_store",
                 "brain.workspace.roots_manager", "brain.workspace.path_sandbox",
                 "brain.workspace.audit_events"]},
    {"key": "trust_and_capabilities", "title": "Trust levels and capabilities",
     "modules": ["brain.workspace.capabilities"]},
    {"key": "portfolios", "title": "Portfolios",
     "modules": ["brain.portfolio.models", "brain.portfolio.store"]},
    {"key": "cross_repository_graph", "title": "Cross-repository graph",
     "modules": ["brain.portfolio.graph_builder"]},
    {"key": "incremental_graph_derivation", "title": "Incremental graph derivation",
     "modules": ["brain.freshness.generation_deriver", "brain.freshness.incremental_planner",
                 "brain.freshness.models"],
     "symbols": ["GenerationDeriver", "IncrementalPlanner", "IncrementalPlan", "DerivationReport",
                 "RebuildDecision"]},
    {"key": "freshness", "title": "Freshness",
     "modules": ["brain.freshness.models", "brain.freshness.tracker"],
     "symbols": ["FreshnessTracker", "FreshnessRecord", "FreshnessState", "InvalidationBus",
                 "InvalidationEvent", "ArtifactType"]},
    {"key": "caches", "title": "Caches",
     "modules": ["brain.freshness.tracker"], "symbols": ["IncrementalCache"]},
    {"key": "change_laboratory", "title": "Change Laboratory",
     "modules": ["brain.lab.laboratory", "brain.lab.engine"],
     "symbols": ["ChangeLaboratory", "PatchApplier", "PatchValidator", "PostPatchAnalyzer",
                 "WorkspaceGuard", "LabSecurityError"]},
    {"key": "validation_profiles", "title": "Validation profiles",
     "modules": ["brain.lab.models"],
     "symbols": ["ValidationProfile", "CommandDef", "ValidationResult", "ValidationRunReport"]},
    {"key": "process_supervision", "title": "Process supervision",
     "modules": ["brain.lab.engine"], "symbols": ["ValidationRunner"]},
    {"key": "workspace_lifecycle", "title": "Disposable workspace lifecycle",
     "modules": ["brain.lab.engine", "brain.lab.models"],
     "symbols": ["WorkspaceManager", "WorkspaceRecord", "WorkspaceState", "CleanupReport",
                 "PatchRecord", "PatchApplyResult", "PostPatchReport", "LabCapability"]},
    {"key": "experiments", "title": "Remediation experiments",
     "modules": ["brain.experiments.models", "brain.experiments.orchestrator",
                 "brain.experiments.packaging"],
     "symbols": ["Experiment", "ExperimentOption", "ExperimentManager", "ExperimentRunner",
                 "ExperimentStore", "ExperimentPackager", "ExperimentState", "HumanAction",
                 "HumanActionRecord"]},
    {"key": "option_comparison", "title": "Option comparison",
     "modules": ["brain.experiments.orchestrator", "brain.experiments.models"],
     "symbols": ["ExperimentComparator", "ComparisonResult", "ExperimentConclusion"]},
    {"key": "patch_export", "title": "Patch export",
     "modules": ["brain.experiments.models"],
     "symbols": ["PatchExport", "ExportStaleError", "ExportNotAuthorizedError"]},
    {"key": "evidence_ledger", "title": "Evidence ledger",
     "modules": ["brain.ledger.models", "brain.ledger.store", "brain.ledger.ledger",
                 "brain.ledger.verifier"]},
    {"key": "control_plane", "title": "Control plane",
     "modules": ["brain.control.plane"], "symbols": ["ControlPlane"]},
    {"key": "action_queue", "title": "Human action queue",
     "modules": ["brain.control.plane"], "symbols": ["ActionItem"]},
    {"key": "budgets", "title": "Budgets",
     "modules": ["brain.control.budgets"]},
]

CLI_MODULES = [
    "brain.workspace.cli",
    "brain.portfolio.cli",
    "brain.freshness.cli",
    "brain.lab.cli",
    "brain.experiments.cli",
    "brain.ledger.cli",
    "brain.control.cli",
]

# API routers introduced by v0.5.0. Older routers stay out of the inventory:
# this release does not own them.
V050_ROUTERS = [
    "workspace", "portfolio", "freshness", "lab", "experiments", "ledger", "control",
]


def _first_line(obj: Any) -> str:
    doc = inspect.getdoc(obj) or ""
    return doc.strip().splitlines()[0] if doc.strip() else ""


def inventory_module(dotted: str, symbols: List[str] | None = None) -> Dict[str, Any]:
    """Import a module and list the public API it actually exposes.

    ``symbols`` narrows the listing to the names a subsystem claims, which is
    what makes shared modules (``brain.lab.engine`` backs three subsystems)
    attributable. A claimed name that the module does not define is reported
    rather than silently dropped.
    """
    module = importlib.import_module(dotted)
    source = Path(inspect.getfile(module))
    classes: List[Dict[str, Any]] = []
    functions: List[Dict[str, Any]] = []
    for name, obj in vars(module).items():
        if name.startswith("_"):
            continue
        if getattr(obj, "__module__", None) != dotted:
            continue  # re-export, owned by another module
        if symbols is not None and name not in symbols:
            continue
        if inspect.isclass(obj):
            methods = sorted(
                n for n, m in vars(obj).items()
                if not n.startswith("_") and (inspect.isfunction(m) or isinstance(m, (staticmethod, classmethod)))
            )
            classes.append({"name": name, "doc": _first_line(obj), "methods": methods})
        elif inspect.isfunction(obj):
            functions.append({
                "name": name,
                "doc": _first_line(obj),
                "signature": str(inspect.signature(obj)),
            })
    return {
        "module": dotted,
        "path": source.relative_to(REPO_ROOT).as_posix(),
        "doc": _first_line(module),
        "lines": len(source.read_text(encoding="utf-8").splitlines()),
        "classes": sorted(classes, key=lambda c: c["name"]),
        "functions": sorted(functions, key=lambda f: f["name"]),
    }


def _walk_parser(parser, path: List[str]) -> List[Dict[str, Any]]:
    """Flatten an argparse tree into one entry per invocable command."""
    import argparse as ap

    commands: List[Dict[str, Any]] = []
    subparser_actions = [a for a in parser._actions if isinstance(a, ap._SubParsersAction)]
    options = sorted(
        opt for a in parser._actions for opt in a.option_strings if opt.startswith("--")
    )
    positionals = [
        a.dest for a in parser._actions
        if not a.option_strings and not isinstance(a, ap._SubParsersAction)
    ]
    if not subparser_actions:
        commands.append({
            "command": " ".join(path),
            "help": (parser.description or "").strip(),
            "options": options,
            "positionals": positionals,
            "handler": getattr(parser.get_default("func"), "__name__", None),
        })
        return commands

    for action in subparser_actions:
        for name, sub in action.choices.items():
            commands.extend(_walk_parser(sub, path + [name]))
    return commands


def inventory_cli(dotted: str) -> Dict[str, Any]:
    """Build the CLI's real parser and walk it."""
    module = importlib.import_module(dotted)
    parser = module.build_parser()
    return {
        "module": dotted,
        "path": Path(inspect.getfile(module)).relative_to(REPO_ROOT).as_posix(),
        "invocation": f"py -3 -m {dotted}",
        "description": (parser.description or "").strip(),
        "commands": sorted(_walk_parser(parser, []), key=lambda c: c["command"]),
    }


def inventory_api() -> List[Dict[str, Any]]:
    """Read the API surface from the application's own OpenAPI document."""
    from apps.api.main import app

    router_prefixes = {}
    for name in V050_ROUTERS:
        module = importlib.import_module(f"apps.api.routers.{name}")
        router_prefixes[name] = module.router.prefix

    endpoints: List[Dict[str, Any]] = []
    for path, operations in app.openapi()["paths"].items():
        owner = next(
            (n for n, prefix in router_prefixes.items() if prefix and path.startswith(prefix)),
            None,
        )
        if owner is None:
            continue
        for method, operation in operations.items():
            endpoints.append({
                "subsystem": owner,
                "method": method.upper(),
                "path": path,
                "operation_id": operation.get("operationId", ""),
                "summary": (operation.get("summary") or "").strip(),
            })
    return sorted(endpoints, key=lambda e: (e["path"], e["method"]))


def _python_sources(*roots: str) -> Iterable[Path]:
    for root in roots:
        for path in (REPO_ROOT / root).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            yield path


def find_unreferenced_modules(modules: List[str]) -> List[str]:
    """v0.5.0 modules that nothing outside themselves imports."""
    haystack = "\n".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in _python_sources("brain", "apps", "tests", "scripts")
    )
    unreferenced = []
    for dotted in modules:
        package, _, leaf = dotted.rpartition(".")
        patterns = [
            rf"import\s+{re.escape(dotted)}\b",
            rf"from\s+{re.escape(dotted)}\s+import",
            rf"from\s+{re.escape(package)}\s+import\s+[^\n]*\b{re.escape(leaf)}\b",
        ]
        references = sum(len(re.findall(p, haystack)) for p in patterns)
        if references == 0:
            unreferenced.append(dotted)
    return unreferenced


def find_unexercised_endpoints(endpoints: List[Dict[str, Any]]) -> List[str]:
    """Endpoints whose path no test mentions."""
    tests = "\n".join(
        p.read_text(encoding="utf-8", errors="replace") for p in _python_sources("tests")
    )
    unexercised = []
    for endpoint in endpoints:
        # Path parameters are filled in by callers, so match on the literal prefix.
        literal = endpoint["path"].split("{")[0].rstrip("/")
        if literal and literal not in tests:
            unexercised.append(f"{endpoint['method']} {endpoint['path']}")
    return sorted(set(unexercised))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Generate the v0.5.0 feature inventory")
    parser.add_argument("--out", default="reports/v0.5.0-release-candidate/feature-inventory.json")
    args = parser.parse_args(argv)

    subsystems = []
    all_modules: List[str] = []
    unresolved: List[str] = []
    for spec in SUBSYSTEMS:
        symbols = spec.get("symbols")
        modules = [inventory_module(m, symbols) for m in spec["modules"]]
        all_modules.extend(spec["modules"])
        found = {c["name"] for m in modules for c in m["classes"]}
        found |= {f["name"] for m in modules for f in m["functions"]}
        missing = sorted(set(symbols or []) - found)
        unresolved.extend(f"{spec['key']}:{name}" for name in missing)
        subsystems.append({
            "key": spec["key"],
            "title": spec["title"],
            "modules": modules,
            "claimed_symbols": sorted(symbols) if symbols else [],
            "unresolved_symbols": missing,
            "class_count": sum(len(m["classes"]) for m in modules),
            "line_count": sum(m["lines"] for m in modules),
        })

    clis = [inventory_cli(m) for m in CLI_MODULES]
    endpoints = inventory_api()

    totals = {
        "subsystems": len(subsystems),
        "modules": len(set(all_modules)),
        "classes": sum(s["class_count"] for s in subsystems),
        "cli_modules": len(clis),
        "cli_commands": sum(len(c["commands"]) for c in clis),
        "api_endpoints": len(endpoints),
    }
    checks = {
        "method": "Literal import and path matching across brain/, apps/, tests/ and scripts/.",
        "unresolved_claimed_symbols": sorted(unresolved),
        "unreferenced_modules": find_unreferenced_modules(sorted(set(all_modules))),
        "endpoints_not_mentioned_in_tests": find_unexercised_endpoints(endpoints),
    }
    inventory = {
        "generated_by": GENERATION_COMMAND,
        "release": "v0.5.0",
        "subsystems": subsystems,
        "cli": clis,
        "api": endpoints,
        "totals": totals,
        "reference_checks": checks,
    }

    out = REPO_ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    # LF explicitly: .gitattributes normalises JSON to LF, so a CRLF file here
    # would hash differently once checked out and break manifest verification.
    out.write_text(
        json.dumps(inventory, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(
        f"{totals['subsystems']} subsystems, {totals['modules']} modules, "
        f"{totals['classes']} classes, {totals['cli_commands']} CLI commands, "
        f"{totals['api_endpoints']} endpoints -> {args.out}"
    )
    if checks["unresolved_claimed_symbols"]:
        print("unresolved symbols: " + ", ".join(checks["unresolved_claimed_symbols"]))
    if checks["unreferenced_modules"]:
        print("unreferenced modules: " + ", ".join(checks["unreferenced_modules"]))
    if checks["endpoints_not_mentioned_in_tests"]:
        print(f"endpoints not mentioned in tests: {len(checks['endpoints_not_mentioned_in_tests'])}")
        for item in checks["endpoints_not_mentioned_in_tests"]:
            print(f"  {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
