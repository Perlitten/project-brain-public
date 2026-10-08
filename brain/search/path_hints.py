"""Task-description heuristics for supplemental retrieval path hints."""

import re
from typing import List


def path_hint_bonus(path: str, hints) -> float:
    """Prefer a concrete file hint over a broad directory substring.

    Qualified file paths are stronger evidence than bare filenames/stems;
    a directory token still supplies the historical modest broad boost.
    """
    normalized = path.replace("\\", "/").lower()
    directory, _, filename = normalized.rpartition("/")
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    stem_path = f"{directory}/{stem}" if directory else stem
    bonus = 0.0
    for hint in hints:
        token = hint.replace("\\", "/").lower().rstrip("/")
        if len(token) < 4 or token not in normalized:
            continue
        bonus = max(bonus, 0.18)
        if token in {normalized, stem_path}:
            bonus = max(bonus, 0.55 if "/" in token else 0.32)
        elif token in {filename, stem}:
            bonus = max(bonus, 0.32)
    return bonus


def derive_path_hints(task_description: str) -> List[str]:
    """Return lowercase path tokens to boost lexical retrieval for common task shapes."""
    lower = task_description.lower()
    hints: List[str] = []

    if "health" in lower or "healthcheck" in lower:
        hints.extend(["health", "test_health", "session", "apps/api/main", "routers/core"])

    if any(token in lower for token in ("retrieval", "search", "hybrid", "context pack")):
        hints.extend(["code_search", "context_pack_builder", "context_pack", "search/"])

    # UI behavior questions span the named component and its style companion.
    # Derive hints from the query instead of baking in one benchmark's
    # component names or repository layout.  The lexical/path matcher can
    # then resolve these stems against the indexed repository.
    if any(token in lower for token in ("tooltip", "tooltips", "style", "stylesheet", "css", "clipping", "overflow")):
        hints.extend([".css", "globals.css", "styles/"])
        for component in re.findall(r"\b[A-Z][A-Za-z0-9_]{1,}\b", task_description):
            hints.append(component.lower())

    if any(token in lower for token in ("dto", "response", "schema", "status endpoint")):
        hints.extend(["brain/database/models", "models.py", "schemas.py", "apps/api/main"])

    if any(token in lower for token in ("branding", "template", "html", "styling", "dashboard")):
        hints.extend(["templates/base", "templates/", "base.html", "apps/api/templates"])

    if "dead code" in lower or "api layer" in lower:
        hints.extend(["apps/api/main", "apps/api/routers", "apps/api/helpers"])

    if "database" in lower and "unavailable" in lower:
        hints.extend(["session", "database", "apps/api/main"])

    if "endpoint" in lower and "api" in lower:
        hints.extend(["apps/api/main", "routers/core", "schemas.py"])

    # Common product-repo surfaces.
    for match in re.finditer(r"\b(?:src|extension|tests|alembic)/[a-z0-9_./-]+", lower):
        hints.append(match.group(0).rstrip("/"))
    for token in re.findall(
        r"\b(engine|server|market|router|recommendation|override|migration|schema|dto|health|deck|battle)\b",
        lower,
    ):
        hints.append(token)
    if "tie-break" in lower or "tiebreak" in lower:
        hints.extend(["test_audit_fixes", "game_engine", "recommendation", "combat_resolution", "tie_break"])
    if "market" in lower:
        hints.extend(["market.py", "market_router", "backfill_market", "ratelimit", "test_endpoints", "MarketTab"])
    if "websocket" in lower or "ws " in lower:
        hints.extend(["websocket.py", "test_websocket"])
    if "rate limit" in lower or "ratelimit" in lower:
        hints.extend(["ratelimit.py", "test_ratelimit"])
    if "health" in lower:
        hints.extend(["health.py", "test_health"])
    if "mcp" in lower:
        hints.extend(["mcp_server.py", "main.py", "run_backend.py"])
    if ("cli" in lower and "graph" in lower) or "symbol graph" in lower or "indexed symbol" in lower:
        hints.extend(["mcp_server.py", "run_backend.py"])
    # Generic CLI / entrypoint surfaces (v6: behavioral queries without file stems)
    if any(
        token in lower
        for token in ("cli", "command-line", "command line", "argparse", "entrypoint",
                      "entry point", "subcommand", "console script")
    ):
        hints.extend(["run_backend.py", "__main__.py", "main.py", "mcp_server.py", "scripts/"])
    if "docker" in lower or "compose" in lower:
        hints.extend(["docker-compose.yml", "docker/"])
    if "nginx" in lower:
        hints.extend(["nginx", ".conf", "deploy/nginx"])
    if "migration" in lower or "database" in lower or "table" in lower or "deck snapshot" in lower:
        hints.extend(["models.py", "migrate.py", "db/", "src/db/"])
    if "analytics" in lower or "event" in lower or "metric" in lower:
        hints.extend(["feedback.py", "logger.py", "log_formatters"])
    if "deck" in lower:
        hints.extend(["decks", "deck"])
    if "recommendation" in lower or "pillz" in lower:
        hints.extend(["recommendation.py", "ai_recommend", "controlled_override"])
    if "/game/state" in lower or "game state" in lower:
        hints.extend(["game_schemas", "game.py", "e2e_stability", "payload_contract"])
    if any(token in lower for token in ("dto", "payload", "openapi", "contract")):
        hints.extend(["game_schemas", "schemas.py", "payload_contract"])

    if "rename" in lower:
        rename_match = re.search(r"rename\s+([a-z0-9_]+)", lower)
        if rename_match:
            hints.append(rename_match.group(1))

    seen = set()
    unique: List[str] = []
    for hint in hints:
        if hint not in seen:
            seen.add(hint)
            unique.append(hint)
    return unique


def expand_keywords(keywords: List[str], task_description: str) -> List[str]:
    """Merge LLM keywords with deterministic path hints."""
    merged = [kw.lower() for kw in keywords]
    for hint in derive_path_hints(task_description):
        if hint not in merged:
            merged.append(hint)
    return merged
