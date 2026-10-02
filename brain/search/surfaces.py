"""Retrieval v6 surface taxonomy.

A *surface* is the structural region of the repository a file belongs to
(engine, server, extension, scripts, tests, docs, config, database). v6 uses
surface labels to (a) give each routed surface a minimum quota in the recall
pool so a dominant surface cannot crowd out tail files, and (b) apply
surface-mismatch penalties during deterministic rerank.

This module is intentionally dependency-free (pure path heuristics) so it can be
imported by filters, task_intent, the pipeline and the reranker without cycles.
"""

from __future__ import annotations

from typing import List

# Canonical surface labels (design §"Surface taxonomy (v6)").
SURFACES = (
    "engine",
    "server",
    "extension",
    "scripts",
    "tests",
    "docs",
    "config",
    "database",
    "other",
)

# v6 P1 structural symbol kinds — surfaced in the lexical symbol channel only when
# RETRIEVAL_V6_ENABLED (keeps v6-off byte-identical to frozen v5 after backfill).
V6_SYMBOL_KINDS = frozenset({
    "api_route",
    "cli_flag",
    "cli_command",
    "script_entrypoint",
})
# Resolved import edges stored as symbols — never in the lexical channel; used only
# for v6 co-rank injection (TEST->SOURCE, SCRIPT->DOMAIN).
IMPORT_TARGET_KIND = "import_target"

_CONFIG_BASENAMES = frozenset({
    "pyproject.toml",
    "package.json",
    "tsconfig.json",
    "docker-compose.yml",
    "compose.yaml",
    "docker-compose.yaml",
    "requirements.txt",
    "setup.cfg",
    "alembic.ini",
})

_DOC_EXTS = (".md", ".rst", ".mdx")
_OPENAPI_HINTS = ("openapi", "swagger")


def _normalize(path: str) -> str:
    return path.replace("\\", "/").lower()


def classify_surface(path: str) -> str:
    """Return the surface label for a repo-relative path (path-derived only)."""
    norm = _normalize(path)
    basename = norm.rsplit("/", 1)[-1]

    # Tests win over their src location (tests/test_engine.py is a test surface).
    parts = norm.split("/")
    if (
        "tests" in parts
        or basename.startswith("test_")
        or basename.endswith("_test.py")
        or ".test." in basename
        or ".spec." in basename
        or norm.startswith("extension/test/")
    ):
        return "tests"

    if basename in _CONFIG_BASENAMES or norm.startswith("deploy/") or norm.startswith("docker/"):
        return "config"

    if any(h in norm for h in _OPENAPI_HINTS):
        return "docs"
    if basename.endswith(_DOC_EXTS):
        return "docs"

    if norm.startswith("src/db/") or "/alembic/" in norm or norm.startswith("alembic/"):
        return "database"

    if norm.startswith("src/engine/") or norm.startswith("src/ml/"):
        return "engine"

    if norm.startswith("src/server/"):
        return "server"

    if norm.startswith("extension/"):
        return "extension"

    # scripts/ tree and root-level python drivers (run_*.py, *_backend.py …).
    if norm.startswith("scripts/"):
        return "scripts"
    if "/" not in norm and norm.endswith(".py"):
        return "scripts"

    return "other"


def route_expected_surface(task_type: str, task_description: str) -> str:
    """Deterministic task → primary expected surface (path-free, intent-derived).

    Returns "" when no surface dominates so the pipeline keeps a general pool.
    """
    lower = task_description.lower()

    if any(t in lower for t in ("extension", "frontend", "tab ", "chrome", "popup", "webpack", "battle tab")):
        return "extension"
    if any(t in lower for t in ("migration", "alembic", "deck_id", "foreign key", "backfill table", "schema table")):
        return "database"
    if task_type == "database":
        return "database"
    if any(t in lower for t in ("nginx", "docker", "compose", "systemd", "deploy", "pyproject")):
        return "config"
    if any(t in lower for t in ("readme", "documentation", "onboarding", "openapi", "guidance")):
        return "docs"
    if any(t in lower for t in ("script", "backfill", "self-play", "selfplay", "harness", "cli ", "entrypoint", "command-line")):
        return "scripts"
    if any(t in lower for t in ("endpoint", "router", "route", "api contract", "dto", "payload")):
        return "server"
    if task_type in {"api_contract"}:
        return "server"
    if any(t in lower for t in ("engine", "solver", "parser", "ability", "simulation", "combat", "win rate", "evaluator")):
        return "engine"
    if task_type in {"bugfix", "analytics"}:
        return "engine"
    return ""


def surfaces_for_pool(expected_surface: str) -> List[str]:
    """Surfaces that must hold a minimum quota in the recall pool for a route.

    Always includes ``tests`` (co-rank companions) plus the routed surface and
    its natural neighbours so multi-file tasks (src+test+schema) keep breadth.
    """
    base = {"tests"}
    if expected_surface:
        base.add(expected_surface)
    neighbours = {
        "server": {"database", "engine"},
        "engine": {"server"},
        "database": {"server"},
        "extension": {"server"},
        "scripts": {"server", "engine"},
        "config": {"server"},
        "docs": {"server"},
    }
    base |= neighbours.get(expected_surface, set())
    return [s for s in SURFACES if s in base]
