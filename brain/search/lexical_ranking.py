"""Lexical path ranking with segment boosts and keyword coverage."""

from __future__ import annotations

import re
from typing import Iterable, List, Sequence, Tuple


_STOPWORDS = frozenset({
    "the", "and", "for", "that", "this", "with", "from", "when", "where",
    "into", "across", "after", "before", "should", "would", "could", "make",
    "add", "fix", "stop", "wire", "emit", "align", "update", "remove", "ensure",
})

# Candidate paths probed when keywords match module/router names
_ROUTER_PROBE = "src/server/routers/{name}.py"
_ENGINE_PROBE = "src/engine/{name}.py"
_TEST_PROBE = "tests/test_{name}.py"


def probe_paths_for_keywords(keywords: Sequence[str], task_description: str = "") -> List[str]:
    """Deterministic high-precision path probes from task keywords."""
    probes: List[str] = []
    seen: set[str] = set()
    kw_set = {k.lower() for k in keywords if k}
    lower_task = task_description.lower()

    def _add(path: str) -> None:
        if path not in seen:
            seen.add(path)
            probes.append(path)

    for raw in keywords:
        kw = raw.lower().strip()
        if not kw or len(kw) < 3 or kw in _STOPWORDS:
            continue
        for template in (_ROUTER_PROBE, _ENGINE_PROBE, _TEST_PROBE):
            _add(template.format(name=kw))
        if kw.endswith(".py"):
            _add(_ROUTER_PROBE.format(name=kw[:-3]))

    if {"game", "engine"} <= kw_set or "game_engine" in lower_task:
        _add("src/engine/game_engine.py")
    rename_match = re.search(r"rename\s+([a-z0-9_]+)\s+module", lower_task)
    if rename_match:
        _add(f"src/engine/{rename_match.group(1)}.py")

    if {"game", "schema"} <= kw_set or {"game", "schemas"} <= kw_set or {"game", "dto"} <= kw_set:
        _add("src/server/routers/game_schemas.py")
        _add("src/server/routers/game.py")
        _add("tests/test_e2e_stability.py")
    if "migration" in lower_task or ("table" in lower_task and "deck" in lower_task):
        _add("src/db/models.py")
        _add("src/db/migrate.py")
        _add("tests/test_migrate_db.py")
    if "market" in kw_set:
        _add("src/server/routers/market.py")
        _add("tests/test_endpoints.py")
        _add("scripts/backfill_market_history.py")
        _add("extension/src/options/tabs/MarketTab.tsx")
    if "market" in lower_task and any(t in lower_task for t in ("stale", "sync", "refresh", "price")):
        _add("scripts/backfill_market_history.py")
        _add("extension/src/options/tabs/MarketTab.tsx")
    if any(t in lower_task for t in ("deprecated", "legacy", "remove")) and "market" in lower_task:
        _add("scripts/backfill_market_history.py")
    if "self-play" in lower_task or "selfplay" in lower_task or "self play" in lower_task:
        _add("src/ml/cfr_self_play.py")
        _add("deploy/scripts/install_server.sh")
    if any(t in lower_task for t in ("colocated", "package boundary", "boundary policy")):
        _add("pyproject.toml")
        _add("tests/")
    if "adr" in lower_task and any(t in lower_task for t in ("engine", "db", "layer", "boundary")):
        _add("tests/test_engine_components.py")
        _add("src/db/models.py")
        _add("src/db/migrate.py")
    if "wire" in lower_task and "battle" in lower_task:
        _add("src/server/routers/game.py")
        _add("tests/test_e2e_stability.py")
    if any(t in lower_task for t in ("deprecated", "legacy", "remove", "adapter")):
        if "market" in lower_task:
            _add("scripts/backfill_market_history.py")
        _add("pyproject.toml")
    if "health" in kw_set:
        _add("src/server/routers/health.py")
        _add("tests/test_health.py")
    if "websocket" in kw_set:
        _add("src/server/routers/websocket.py")
        _add("tests/test_websocket.py")
    if "mcp" in kw_set or "cli" in kw_set or ("symbol" in kw_set and "graph" in kw_set):
        _add("src/server/mcp_server.py")
        _add("run_backend.py")
    if any(t in lower_task for t in ("cli command", "symbol graph", "indexed symbol")):
        _add("src/server/mcp_server.py")
        _add("run_backend.py")
    if "battle" in lower_task and any(t in lower_task for t in ("tab", "extension", "wire", "endpoint", "recommendation")):
        _add("src/server/routers/game.py")
        _add("extension/src/options/tabs/BattleTab.tsx")
    if any(t in lower_task for t in ("adr", "boundary", "layer")) and "engine" in lower_task:
        _add("src/db/models.py")
        _add("src/db/migrate.py")
    if "backfill" in lower_task and "deck" in lower_task:
        _add("scripts/backfill_db.py")
        _add("src/db/models.py")
        _add("src/db/migrate.py")
    if "tie-break" in lower_task or "tiebreak" in lower_task or "double-count" in lower_task:
        _add("tests/test_audit_fixes.py")
    if "game" in lower_task and "log" in lower_task:
        _add("tests/test_game.py")
    if "meta" in lower_task and "dashboard" in lower_task:
        _add("src/server/routers/meta.py")
    if "round end" in lower_task or "duplicate event" in lower_task:
        _add("tests/test_decision_logging.py")
    if "backfill" in lower_task and any(t in lower_task for t in ("deck", "match", "historical", "null")):
        _add("src/db/models.py")
        _add("src/db/migrate.py")
    if "override" in lower_task and any(t in lower_task for t in ("dashboard", "analytics", "rate", "query")):
        _add("src/server/routers/meta.py")
        _add("src/server/routers/feedback.py")
    if "recommendation" in lower_task:
        _add("src/engine/recommendation.py")

    return probes


def _path_segments(path: str) -> List[str]:
    normalized = path.replace("\\", "/").lower()
    parts = re.split(r"[/_.-]+", normalized)
    return [p for p in parts if len(p) >= 2]


def _stem(filename: str) -> str:
    lower = filename.lower()
    for suffix in (".py", ".ts", ".tsx", ".js", ".css", ".md", ".yaml", ".yml", ".sh", ".conf"):
        if lower.endswith(suffix):
            return lower[: -len(suffix)]
    return lower


def score_path_for_keywords(
    path: str,
    keywords: Sequence[str],
    task_description: str = "",
) -> float:
    """Score a repository-relative path for keyword relevance."""
    if not keywords:
        return 0.0

    normalized = path.replace("\\", "/").lower()
    segments = set(_path_segments(path))
    basename = normalized.rsplit("/", 1)[-1]
    stem = _stem(basename)
    compact_path = normalized.replace("/", "").replace("_", "").replace("-", "")

    score = 0.0
    matched = 0
    for raw_kw in keywords:
        kw = raw_kw.lower().strip()
        if not kw or len(kw) < 2 or kw in _STOPWORDS:
            continue

        if kw == stem or kw == basename:
            score += 6.0
            matched += 1
            continue

        if basename == f"{kw}.py" or basename == f"{kw}.ts" or basename == f"{kw}.tsx":
            score += 5.5
            matched += 1
            continue

        if kw in segments:
            score += 4.0
            matched += 1
            continue

        if kw in normalized:
            score += 2.0
            matched += 1
            continue

        compact_kw = kw.replace("_", "").replace("-", "")
        if len(compact_kw) >= 4 and compact_kw in compact_path:
            score += 1.2
            matched += 1

    if matched == 0:
        return 0.0

    coverage = matched / max(len([k for k in keywords if k and len(k) >= 2]), 1)
    score += coverage * 2.5

    # Compound path relevance: multiple keywords in path segments
    kw_in_path = sum(1 for kw in keywords if len(kw) >= 3 and kw in normalized)
    if kw_in_path >= 2:
        score += kw_in_path * 1.5

    lower_task = task_description.lower()
    if "test" in lower_task or "bug" in lower_task or "fix" in lower_task:
        if normalized.startswith("tests/") or "/test_" in normalized:
            score += 1.5
    if any(token in lower_task for token in ("router", "endpoint", "api", "dto", "schema")):
        if "/routers/" in normalized or "schemas" in basename:
            score += 1.5
    if "extension" in lower_task or "tab" in lower_task or "theme" in lower_task:
        if normalized.startswith("extension/"):
            score += 2.0

    return score


def rank_paths(
    paths: Iterable[str],
    keywords: Sequence[str],
    task_description: str = "",
) -> List[Tuple[str, float]]:
    """Return paths sorted by descending lexical score."""
    scored = [(p, score_path_for_keywords(p, keywords, task_description)) for p in paths]
    scored = [(p, s) for p, s in scored if s > 0]
    scored.sort(key=lambda item: (-item[1], item[0]))
    return scored
