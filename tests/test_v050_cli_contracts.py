"""RC Phase 3 — contract smoke tests for every v0.5.0 command-line interface.

One invocation per command, run in-process against the real five-repository
fixture portfolio inside ``tmp_path``. The CLIs resolve their stores relative
to the working directory, so the chdir is what keeps each run's state
disposable; nothing here touches the authoritative Project Brain checkout.

``test_every_v050_cli_command_is_covered`` is the gate: a command added to any
of the seven parsers without a smoke test fails this module.
"""

from __future__ import annotations

import argparse
import difflib
import importlib
import json
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

import pytest

from tests.support.portfolio_fixture import ROLES, build_fixture_portfolio

#: Every module that ships a v0.5.0 CLI.
CLI_MODULES = (
    "brain.workspace.cli",
    "brain.portfolio.cli",
    "brain.freshness.cli",
    "brain.lab.cli",
    "brain.experiments.cli",
    "brain.ledger.cli",
    "brain.control.cli",
)

#: (module, command) pairs this module claims to exercise. Kept honest from
#: both sides: the gate compares it against the parsers, and against what the
#: tests actually invoked.
COVERED_COMMANDS: Set[Tuple[str, str]] = {
    ("brain.workspace.cli", "repositories register"),
    ("brain.workspace.cli", "repositories list"),
    ("brain.workspace.cli", "repositories show"),
    ("brain.workspace.cli", "repositories capabilities"),
    ("brain.workspace.cli", "repositories events"),
    ("brain.workspace.cli", "repositories verify"),
    ("brain.workspace.cli", "repositories disable"),
    ("brain.portfolio.cli", "create"),
    ("brain.portfolio.cli", "list"),
    ("brain.portfolio.cli", "summary"),
    ("brain.portfolio.cli", "build"),
    ("brain.portfolio.cli", "dependencies"),
    ("brain.portfolio.cli", "cycles"),
    ("brain.portfolio.cli", "coupling"),
    ("brain.freshness.cli", "rebuild"),
    ("brain.freshness.cli", "status"),
    ("brain.freshness.cli", "plan"),
    ("brain.freshness.cli", "explain"),
    ("brain.lab.cli", "profiles"),
    ("brain.lab.cli", "workspaces"),
    ("brain.lab.cli", "validate"),
    ("brain.lab.cli", "session"),
    ("brain.lab.cli", "clean"),
    ("brain.lab.cli", "recover"),
    ("brain.experiments.cli", "create"),
    ("brain.experiments.cli", "run"),
    ("brain.experiments.cli", "show"),
    ("brain.experiments.cli", "list"),
    ("brain.experiments.cli", "compare"),
    ("brain.experiments.cli", "recommend"),
    ("brain.experiments.cli", "action"),
    ("brain.experiments.cli", "export"),
    ("brain.experiments.cli", "package"),
    ("brain.experiments.cli", "cancel"),
    ("brain.ledger.cli", "health"),
    ("brain.ledger.cli", "verify"),
    ("brain.ledger.cli", "events"),
    ("brain.ledger.cli", "show"),
    ("brain.ledger.cli", "entity-history"),
    ("brain.ledger.cli", "export"),
    ("brain.control.cli", "summary"),
    ("brain.control.cli", "queue"),
    ("brain.control.cli", "budgets"),
    ("brain.control.cli", "check-budget"),
    ("brain.control.cli", "metrics"),
    ("brain.control.cli", "health"),
    ("brain.control.cli", "write-budgets"),
}

#: Filled in by the ``cli`` fixture as the suite runs.
INVOKED: Set[Tuple[str, str]] = set()


# ── parser introspection ──


def _subparsers(parser: argparse.ArgumentParser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _walk(parser: argparse.ArgumentParser, prefix: Tuple[str, ...] = ()) -> List[str]:
    """Every invocable command path under ``parser``."""
    sub = _subparsers(parser)
    if sub is None:
        return [" ".join(prefix)] if prefix else []
    commands: List[str] = []
    for name, child in sub.choices.items():
        commands.extend(_walk(child, prefix + (name,)))
    return commands


def _command_of(module_name: str, argv: Sequence[str]) -> str:
    """The command path ``argv`` selects, resolved against the real parser."""
    parser = importlib.import_module(module_name).build_parser()
    parts: List[str] = []
    for token in argv:
        sub = _subparsers(parser)
        if sub is None:
            break
        if token in sub.choices:
            parts.append(token)
            parser = sub.choices[token]
    return " ".join(parts)


# ── invocation ──


@pytest.fixture
def cli(capsys):
    """Run a CLI in-process and return its stdout.

    In-process rather than by subprocess so the working directory, the
    temporary brain directory, and coverage all apply. ``main`` either returns
    an exit code or raises ``SystemExit``; both are normalised here.
    """

    def _run(module_name: str, argv: Sequence[str], expect: Optional[int] = 0) -> str:
        module = importlib.import_module(module_name)
        INVOKED.add((module_name, _command_of(module_name, argv)))
        code = 0
        try:
            result = module.main(list(argv))
            if isinstance(result, int):
                code = result
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        captured = capsys.readouterr()
        if expect is not None:
            assert code == expect, (
                f"{module_name} {' '.join(argv)} exited {code}\n"
                f"stdout:\n{captured.out}\nstderr:\n{captured.err}"
            )
        return captured.out

    return _run


def _json(out: str):
    """The JSON document a CLI printed, ignoring any human-readable preamble."""
    start = min((i for i in (out.find("{"), out.find("[")) if i >= 0), default=-1)
    assert start >= 0, f"no JSON in output:\n{out}"
    return json.loads(out[start:])


def _patch_adding_line(repo: Path, rel_path: str, text: str) -> str:
    """A unified diff with real context, derived from the file on disk.

    ``git apply`` rejects zero-context hunks unless ``--unidiff-zero`` is
    passed, which the product deliberately does not do.
    """
    before = (repo / rel_path).read_text(encoding="utf-8").splitlines(keepends=True)
    after = [before[0], text + "\n"] + before[1:]
    body = "".join(
        difflib.unified_diff(before, after, fromfile=f"a/{rel_path}", tofile=f"b/{rel_path}")
    )
    return f"diff --git a/{rel_path} b/{rel_path}\n{body}"


def _is_clean(repo: Path) -> bool:
    result = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
    )
    return result.stdout.strip() == ""


# ── shared environment ──


@pytest.fixture
def portfolio(tmp_path, monkeypatch):
    """Five real Git repositories, with the CLIs rooted in ``tmp_path``."""
    built = build_fixture_portfolio(tmp_path / "fixtures")
    monkeypatch.chdir(tmp_path)
    return built


@pytest.fixture
def registered(portfolio, cli) -> Dict[str, str]:
    """Alias → repository id, registered through the CLI itself.

    ``trusted_internal`` rather than ``fixture``: the export path requires the
    ``export_patch`` capability, which the fixture trust level does not grant.
    """
    ids: Dict[str, str] = {}
    for alias in ROLES:
        out = cli(
            "brain.workspace.cli",
            [
                "repositories",
                "register",
                "--path",
                str(portfolio.path(alias)),
                "--name",
                alias,
                "--trust",
                "trusted_internal",
                "--org",
                "brain-fixtures",
                "--owner",
                "platform-team",
            ],
        )
        ids[alias] = _json(out)["repository_id"]
    return ids


# ── brain.workspace.cli ──


def test_repositories_register_list_and_show(portfolio, registered, cli):
    listed = _json(cli("brain.workspace.cli", ["repositories", "list", "--format", "json"]))
    assert {r["repository_id"] for r in listed} == set(registered.values())

    shown = _json(cli("brain.workspace.cli", ["repositories", "show", registered["api-service"]]))
    assert shown["display_name"] == "api-service"
    # Registration resolves the revision; it never stores an unresolvable marker.
    assert shown["current_revision"] == portfolio.head("api-service")
    assert shown["trust_level"] == "trusted_internal"


def test_repositories_capabilities_events_and_verify(registered, cli):
    caps = _json(
        cli("brain.workspace.cli", ["repositories", "capabilities", registered["web-client"]])
    )
    assert "apply_candidate_patches" in caps["capabilities"]
    assert caps["validation_issues"] == []

    events = _json(
        cli(
            "brain.workspace.cli",
            ["repositories", "events", "--repository-id", registered["web-client"], "--limit", "10"],
        )
    )
    assert [e["event_type"] for e in events] == ["registered"]

    assert "OK" in cli("brain.workspace.cli", ["repositories", "verify"])


def test_repositories_disable_is_recorded(registered, cli):
    repo_id = registered["deployment-config"]
    cli(
        "brain.workspace.cli",
        ["repositories", "disable", repo_id, "--reason", "smoke test"],
    )
    assert _json(cli("brain.workspace.cli", ["repositories", "show", repo_id]))["disabled"] is True


# ── brain.portfolio.cli ──


def _portfolio_config(registered: Dict[str, str]) -> dict:
    return {
        "version": 1,
        "portfolio": {
            "name": "orders-platform-cli-smoke",
            "owners": ["platform-team"],
            "architecture_policy": "clients call APIs; only services import contracts",
        },
        "repositories": {
            alias: {"repository_id": repo_id, "role": ROLES[alias], "description": alias}
            for alias, repo_id in registered.items()
        },
        "contracts": [
            {
                "from": "api-service",
                "to": "shared-contracts",
                "type": "depends_on_package",
                "allowed": True,
                "description": "api-service consumes the contracts package",
            },
            {
                "from": "worker-service",
                "to": "shared-contracts",
                "type": "consumes_event",
                "allowed": True,
                "description": "worker-service consumes orders.created",
            },
            {
                "from": "web-client",
                "to": "api-service",
                "type": "calls_api",
                "allowed": True,
                "description": "web-client calls GET /orders",
            },
            {
                "from": "web-client",
                "to": "worker-service",
                "type": "depends_on_package",
                "allowed": False,
                "description": "a client must not import worker source directly",
            },
        ],
    }


@pytest.fixture
def portfolio_id(tmp_path, registered, cli) -> str:
    import yaml

    config_path = tmp_path / "portfolio.yaml"
    config_path.write_text(
        yaml.safe_dump(_portfolio_config(registered), sort_keys=False), encoding="utf-8"
    )
    created = _json(cli("brain.portfolio.cli", ["create", "--config", str(config_path)]))
    return created["portfolio_id"]


def test_portfolio_create_list_and_summary(portfolio_id, cli):
    listed = _json(cli("brain.portfolio.cli", ["list", "--format", "json"]))
    assert [p["portfolio_id"] for p in listed] == [portfolio_id]

    summary = _json(cli("brain.portfolio.cli", ["summary", portfolio_id]))
    assert {r["alias"] for r in summary["repositories"]} == set(ROLES)


def test_portfolio_build_dependencies_cycles_and_coupling(portfolio, portfolio_id, cli):
    built = cli("brain.portfolio.cli", ["build", portfolio_id])
    assert "Built generation:" in built

    deps = _json(cli("brain.portfolio.cli", ["dependencies", portfolio_id]))
    assert deps["total"] == len(deps["dependencies"]) > 0

    cycles = _json(cli("brain.portfolio.cli", ["cycles", portfolio_id]))
    assert cycles["circular_dependencies"] == []

    coupling = _json(cli("brain.portfolio.cli", ["coupling", portfolio_id]))
    assert "circular_dependencies" in coupling


def test_portfolio_build_refuses_unregistered_repositories(tmp_path, registered, cli):
    """A generation records the revision it was built from, so an unregistered
    repository has to fail loudly rather than be recorded as ``HEAD``."""
    import yaml

    config = _portfolio_config(registered)
    config["portfolio"]["name"] = "portfolio-with-a-ghost"
    config["repositories"]["ghost"] = {"repository_id": "repo-does-not-exist", "role": "service"}
    config_path = tmp_path / "ghost.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    ghost_id = _json(cli("brain.portfolio.cli", ["create", "--config", str(config_path)]))[
        "portfolio_id"
    ]
    cli("brain.portfolio.cli", ["build", ghost_id], expect=1)


# ── brain.freshness.cli ──


def test_freshness_rebuild_status_plan_and_explain(portfolio, registered, cli):
    repo_id = registered["api-service"]
    base = portfolio.head("api-service")

    rebuilt = _json(cli("brain.freshness.cli", ["rebuild", "--repository", repo_id]))
    assert rebuilt["derivation"]["decision"] in {"full_rebuild", "incremental"}
    assert rebuilt["derivation"]["validation_errors"] == []

    status = _json(cli("brain.freshness.cli", ["status", "--repository", repo_id]))
    assert status["observed_revision"] == base
    assert status["graph_freshness"] == "current"

    candidate = portfolio.commit(
        "api-service", {"api/routes.py": "ROUTE = \"GET /v2/orders\"\n"}, "change: v2 route"
    )
    plan = _json(
        cli(
            "brain.freshness.cli",
            [
                "plan",
                "--repository",
                repo_id,
                "--base",
                base,
                "--candidate",
                candidate,
            ],
        )
    )
    assert plan["plan"]["decision"] in {"full_rebuild", "incremental"}
    assert plan["plan"]["modified_files"] == ["api/routes.py"]
    assert plan["preview"]

    explained = _json(cli("brain.freshness.cli", ["explain", "--artifact", f"graph:{repo_id}"]))
    assert explained["found"] is True

    cli("brain.freshness.cli", ["explain", "--artifact", "graph:missing"], expect=1)


# ── brain.lab.cli ──


def test_lab_profiles_and_workspaces_start_empty(registered, cli):
    profiles = _json(cli("brain.lab.cli", ["profiles"]))
    assert {"python-compile", "python-standard"} <= set(profiles["profiles"])

    workspaces = _json(cli("brain.lab.cli", ["workspaces"]))
    assert workspaces["workspaces"] == []


def test_lab_validate_session_clean_and_recover(portfolio, registered, cli, tmp_path):
    repo = portfolio.path("web-client")
    patch_file = tmp_path / "note.diff"
    patch_file.write_text(
        _patch_adding_line(repo, "README.md", "Validated in a disposable workspace."),
        encoding="utf-8",
    )

    session = _json(
        cli(
            "brain.lab.cli",
            [
                "validate",
                "--repository",
                registered["web-client"],
                "--patch-file",
                str(patch_file),
                "--profile",
                "python-compile",
                "--keep-workspace",
            ],
        )
    )
    assert session["outcome"] == "passed"
    assert session["apply"]["success"] is True
    # Never claim isolation that was not verified at the OS level: the session
    # reports the mechanism actually used, enforced:<backend> or unverified.
    assert session["network_isolation"] in {"unverified", "enforced:docker", "enforced:unshare"}
    # The patch lived only in the workspace copy.
    assert _is_clean(repo)
    assert "Validated in a disposable workspace." not in (repo / "README.md").read_text(
        encoding="utf-8"
    )

    stored = _json(cli("brain.lab.cli", ["session", "--session", session["session_id"]]))
    assert stored["session_id"] == session["session_id"]

    listed = _json(cli("brain.lab.cli", ["workspaces"]))
    assert session["workspace_id"] in {w["workspace_id"] for w in listed["workspaces"]}

    cleaned = _json(
        cli(
            "brain.lab.cli",
            ["clean", "--workspace", session["workspace_id"], "--reason", "cli smoke"],
        )
    )
    assert cleaned["removed"] is True

    recovered = _json(cli("brain.lab.cli", ["recover"]))
    assert recovered["orphan_directories"] == []
    assert recovered["missing_workspaces"] == []


def test_lab_session_rejects_an_unknown_id(registered, cli):
    cli("brain.lab.cli", ["session", "--session", "session-does-not-exist"], expect=1)


# ── brain.experiments.cli ──


@pytest.fixture
def experiment(portfolio, registered, cli, tmp_path) -> str:
    """A two-option experiment over the shared-contracts README."""
    repo = portfolio.path("shared-contracts")
    option_a = tmp_path / "option-a.diff"
    option_b = tmp_path / "option-b.diff"
    option_a.write_text(_patch_adding_line(repo, "README.md", "Option A."), encoding="utf-8")
    option_b.write_text(_patch_adding_line(repo, "README.md", "Option B."), encoding="utf-8")

    created = _json(
        cli(
            "brain.experiments.cli",
            [
                "create",
                "--repository",
                registered["shared-contracts"],
                "--option",
                str(option_a),
                "--option",
                str(option_b),
                "--profile",
                "python-compile",
                "--actor",
                "cli-smoke",
            ],
        )
    )
    assert [o["option_id"] for o in created["options"]] == ["opt-1", "opt-2"]
    return created["experiment_id"]


def test_experiment_run_compare_and_recommend(portfolio, experiment, cli):
    run = _json(cli("brain.experiments.cli", ["run", experiment]))
    assert run["state"] in {"completed", "comparing", "concluded"}
    assert all(o["executed"] for o in run["options"])
    assert all(o["workspace_id"] for o in run["options"])

    shown = _json(cli("brain.experiments.cli", ["show", experiment]))
    assert shown["experiment_id"] == experiment

    listed = _json(cli("brain.experiments.cli", ["list"]))
    assert experiment in {e["experiment_id"] for e in listed["experiments"]}

    comparison = _json(cli("brain.experiments.cli", ["compare", experiment]))
    assert comparison["formula"]
    assert len(comparison["rankings"]) == 2

    recommendation = _json(cli("brain.experiments.cli", ["recommend", experiment]))
    assert recommendation["conclusion"]
    # A recommendation is not an approval.
    assert recommendation["approval_status"] == "not_approved"

    # Every option was validated in its own disposable workspace.
    assert _is_clean(portfolio.path("shared-contracts"))


def test_experiment_export_requires_a_human_decision_and_never_applies(
    portfolio, experiment, cli, tmp_path
):
    cli("brain.experiments.cli", ["run", experiment])
    cli("brain.experiments.cli", ["compare", experiment])

    # Export before anyone accepted the option is refused.
    cli(
        "brain.experiments.cli",
        ["export", experiment, "--option-id", "opt-1"],
        expect=1,
    )

    cli(
        "brain.experiments.cli",
        [
            "action",
            experiment,
            "--action",
            "accept_for_export",
            "--option-id",
            "opt-1",
            "--actor",
            "operator",
            "--note",
            "cli smoke",
        ],
    )

    out_dir = tmp_path / "export"
    export = _json(
        cli(
            "brain.experiments.cli",
            ["export", experiment, "--option-id", "opt-1", "--out", str(out_dir)],
        )
    )
    assert export["unified_diff"]
    assert list(out_dir.glob("*.diff"))
    assert list(out_dir.glob("*.json"))

    # Exporting packages a patch for a human; it does not touch the checkout.
    repo = portfolio.path("shared-contracts")
    assert _is_clean(repo)
    assert "Option A." not in (repo / "README.md").read_text(encoding="utf-8")

    package = _json(
        cli(
            "brain.experiments.cli",
            ["package", experiment, "--reports-dir", str(tmp_path / "reports")],
        )
    )
    assert Path(package["package_dir"]).is_dir()
    assert package["manifest"]


def test_experiment_cancel(experiment, cli):
    cancelled = _json(cli("brain.experiments.cli", ["cancel", experiment]))
    assert cancelled["cancel_requested"] is True
    assert cancelled["state"] == "cancelled"


# ── brain.ledger.cli ──


@pytest.fixture
def ledger_events(portfolio, registered, tmp_path):
    """Two real product events, written through the named recorders.

    The ledger has no generic writer, so a smoke test seeds it the same way the
    product does.
    """
    from brain.ledger.ledger import EvidenceLedger

    ledger = EvidenceLedger(Path(".brain"))
    first = ledger.record_repository_registered(
        registered["api-service"],
        actor_identity="cli-smoke",
        trust_level="trusted_internal",
        source_revision=portfolio.head("api-service"),
    )
    second = ledger.record_graph_generation_built(
        registered["api-service"],
        "gen-cli-smoke",
        source_revision=portfolio.head("api-service"),
    )
    return first, second


def test_ledger_health_verify_and_queries(ledger_events, registered, cli, tmp_path):
    first, _second = ledger_events

    health = _json(cli("brain.ledger.cli", ["health"]))
    assert health["valid"] is True
    assert health["events"] == 2

    verified = _json(cli("brain.ledger.cli", ["verify"]))
    assert verified["valid"] is True
    assert verified["source"] == "database"

    events = _json(
        cli(
            "brain.ledger.cli",
            ["events", "--repository-id", registered["api-service"], "--limit", "10"],
        )
    )
    assert len(events["events"]) == 2

    shown = _json(cli("brain.ledger.cli", ["show", first.event_id]))
    assert shown["hash_valid"] is True

    history = _json(
        cli(
            "brain.ledger.cli",
            ["entity-history", "repository", registered["api-service"]],
        )
    )
    assert history["entity_id"] == registered["api-service"]
    assert history["events"]

    export_path = tmp_path / "ledger.jsonl"
    exported = _json(cli("brain.ledger.cli", ["export", "--output", str(export_path)]))
    assert exported["events"] == 2
    assert export_path.is_file()

    from_export = _json(cli("brain.ledger.cli", ["verify", "--input", str(export_path)]))
    assert from_export["valid"] is True


def test_ledger_verify_fails_on_a_tampered_export(ledger_events, cli, tmp_path):
    """A tampered chain must fail the shell, not merely print prose."""
    export_path = tmp_path / "ledger.jsonl"
    cli("brain.ledger.cli", ["export", "--output", str(export_path)])

    lines = export_path.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["actor_identity"] = "someone-else"
    lines[0] = json.dumps(first, sort_keys=True)
    export_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    report = _json(cli("brain.ledger.cli", ["verify", "--input", str(export_path)], expect=1))
    assert report["valid"] is False


# ── brain.control.cli ──


def test_control_summary_queue_and_metrics(registered, ledger_events, cli):
    summary = _json(cli("brain.control.cli", ["summary"]))
    assert summary["repositories"]["total"] == len(ROLES)

    queue = _json(cli("brain.control.cli", ["queue", "--max-priority", "4"]))
    assert queue["total"] == len(queue["items"])

    metrics = _json(cli("brain.control.cli", ["metrics"]))
    assert metrics["ledger_valid"] is True
    assert metrics["ledger_events"] == 2

    # Exit code carries the verdict, so accept either and check the payload.
    health = _json(cli("brain.control.cli", ["health"], expect=None))
    assert health["status"] in {"ok", "degraded", "unhealthy"}


def test_control_budgets_are_fail_closed(registered, cli, tmp_path):
    budgets = _json(cli("brain.control.cli", ["budgets"]))
    assert "max_active_workspaces" in budgets["policy"]
    assert {u["budget"] for u in budgets["usage"]} == set(budgets["policy"])

    allowed = _json(
        cli("brain.control.cli", ["check-budget", "max_active_workspaces", "--requested", "1"])
    )
    assert allowed["allowed"] is True

    # An unknown budget is refused rather than defaulted.
    refused = _json(cli("brain.control.cli", ["check-budget", "no-such-budget"], expect=1))
    assert refused["allowed"] is False

    # Far beyond any configured ceiling.
    over = _json(
        cli(
            "brain.control.cli",
            ["check-budget", "max_active_workspaces", "--requested", "10000"],
            expect=1,
        )
    )
    assert over["allowed"] is False

    written = _json(cli("brain.control.cli", ["write-budgets"]))
    assert written["written"] is True
    assert Path(written["path"]).is_file()

    # Writing over an existing policy needs --force.
    again = _json(cli("brain.control.cli", ["write-budgets"], expect=1))
    assert again["written"] is False


# ── coverage gate ──


def test_every_v050_cli_command_is_covered(request):
    """A command added to any v0.5.0 parser must arrive with a smoke test."""
    live = {
        (module_name, command)
        for module_name in CLI_MODULES
        for command in _walk(importlib.import_module(module_name).build_parser())
    }
    assert live - COVERED_COMMANDS == set(), "commands without a smoke test"
    assert COVERED_COMMANDS - live == set(), "smoke tests for commands that no longer exist"

    # The claim above is only worth anything if the tests really ran them, so
    # check it whenever the whole module was selected.
    defined = sum(
        1 for name, obj in globals().items() if name.startswith("test_") and callable(obj)
    )
    selected = [item for item in request.session.items if item.fspath == request.node.fspath]
    if len(selected) >= defined:
        assert COVERED_COMMANDS - INVOKED == set(), "commands claimed but never invoked"
