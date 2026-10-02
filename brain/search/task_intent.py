"""Deterministic task-intent classification for retrieval ranking (no LLM)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from brain.search.surfaces import route_expected_surface


@dataclass(frozen=True)
class TaskIntent:
    task_type: str
    wants_extension: bool
    wants_deploy: bool
    wants_scripts: bool
    wants_ml: bool
    wants_root_config: bool
    wants_colocated_tests: bool
    wants_server_router: bool
    is_deletion: bool
    wants_cli: bool = False
    expected_surface: str = ""


_ROOT_CONFIG_FILES = frozenset({
    "pyproject.toml",
    "package.json",
    "tsconfig.json",
    "docker-compose.yml",
    "compose.yaml",
})


def classify_task_type(task_description: str, category: str = "") -> str:
    lower = task_description.lower()
    if category == "deletion_rename":
        return "deletion_rename"
    if category == "ambiguous":
        return "ambiguous"
    if any(token in lower for token in ("bug", "fix", "issue", "repair", "stop", "miscalculation", "correct")):
        return "bugfix"
    if "refactor" in lower or "rename" in lower:
        return "refactor"
    if any(token in lower for token in ("design", "ui", "style", "branding", "template", "theme", "badge")):
        return "design_change"
    if any(token in lower for token in ("migration", "table", "foreign key", "backfill", "deck_id", "database")):
        return "database"
    if any(token in lower for token in ("openapi", "dto", "schema", "contract", "payload", "breaking change")):
        return "api_contract"
    if any(token in lower for token in ("nginx", "docker", "extension", "across", "coordinate", "compose", "wire")):
        return "cross_surface"
    if any(token in lower for token in ("analytics", "event", "metric", "histogram", "dashboard", "override rate")):
        return "analytics"
    if any(token in lower for token in ("adr", "boundary", "enforce", "policy", "colocated", "ownership", "package boundary")):
        return "architecture_rule"
    if any(token in lower for token in ("feat", "add", "emit", "align", "expose", "cli")):
        return "feature"
    return category or "other"


def derive_task_intent(task_description: str, task_type: str = "", category: str = "") -> TaskIntent:
    lower = task_description.lower()
    resolved_type = task_type or classify_task_type(task_description, category)

    wants_extension = any(
        token in lower
        for token in (
            "extension", "frontend", "tab", "ui", "theme", "badge", "chrome",
            "popup", "dark mode", "webpack", "battle tab",
        )
    ) or resolved_type in {"design_change", "cross_surface"}

    wants_deploy = any(
        token in lower for token in ("nginx", "docker", "compose", "deploy", "systemd", "install_server")
    )

    wants_scripts = (
        resolved_type in {"deletion_rename", "database"}
        or any(
            token in lower
            for token in (
                "script", "backfill", "install", "self-play", "selfplay", "self play",
                "adapter", "legacy", "deprecated", "remove module", "harness",
            )
        )
    )

    wants_ml = any(
        token in lower
        for token in ("ml", "cfr", "training pipeline", "self-play", "selfplay", "model training")
    )

    wants_root_config = (
        resolved_type == "architecture_rule"
        or any(token in lower for token in ("pyproject", "colocated", "package boundary", "boundary policy"))
    )

    wants_colocated_tests = wants_root_config or "colocated" in lower

    wants_server_router = (
        any(token in lower for token in ("endpoint", "router", "/game/", "recommendation endpoint", "server"))
        and wants_extension
    ) or resolved_type in {"api_contract", "cross_surface", "feature"}

    is_deletion = resolved_type == "deletion_rename" or any(
        token in lower for token in ("remove", "deprecated", "legacy", "delete")
    )

    wants_cli = any(
        token in lower
        for token in (
            "cli", "command-line", "command line", "argparse", "entrypoint",
            "entry point", "run_backend", "subcommand", "--flag", "typer", "click command",
        )
    )

    expected_surface = route_expected_surface(resolved_type, task_description)

    return TaskIntent(
        task_type=resolved_type,
        wants_extension=wants_extension,
        wants_deploy=wants_deploy,
        wants_scripts=wants_scripts,
        wants_ml=wants_ml,
        wants_root_config=wants_root_config,
        wants_colocated_tests=wants_colocated_tests,
        wants_server_router=wants_server_router,
        is_deletion=is_deletion,
        wants_cli=wants_cli,
        expected_surface=expected_surface,
    )


def is_root_config_path(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    basename = normalized.rsplit("/", 1)[-1]
    return basename in _ROOT_CONFIG_FILES


def audit_test_boost_paths(task_description: str) -> List[str]:
    lower = task_description.lower()
    paths: List[str] = []
    if any(token in lower for token in ("pillz", "tie-break", "tiebreak", "audit", "recommendation", "override")):
        paths.append("tests/test_audit_fixes.py")
    if "e2e" in lower or ("wire" in lower and "endpoint" in lower):
        paths.append("tests/test_e2e_stability.py")
    if any(token in lower for token in ("adr", "engine layer", "boundary", "engine vs")):
        paths.append("tests/test_engine_components.py")
    return paths
