"""Tests for lexical path ranking helpers."""

from brain.search.lexical_ranking import probe_paths_for_keywords, score_path_for_keywords
from brain.search.filters import (
    enforce_precision_top_k,
    is_extension_noise_path,
    is_script_noise_path,
    trim_ranked_files,
)


def test_market_path_scores_high_for_market_task():
    score = score_path_for_keywords(
        "src/server/routers/market.py",
        ["market", "stale", "price", "sync"],
        "Stop market listing refresh from returning stale card prices after sync",
    )
    noise = score_path_for_keywords(
        "extension/src/content/injected.js",
        ["market", "stale", "price", "sync"],
        "Stop market listing refresh from returning stale card prices after sync",
    )
    assert score > noise


def test_extension_noise_detection():
    assert is_extension_noise_path("extension/src/content/injected.js")
    assert not is_extension_noise_path("src/server/routers/market.py")


def test_deep_budget_skips_aggressive_trim():
    scored = [(1.0, "a.py", {}), (0.9, "b.py", {}), (0.3, "c.py", {}), (0.25, "d.py", {})]
    deep = trim_ranked_files(scored, 10, aggressive=False)
    assert len(deep) == 4
    shallow = trim_ranked_files(scored, 10, aggressive=True)
    assert len(shallow) < len(deep)


def test_cli_graph_probe_paths():
    probes = probe_paths_for_keywords(
        ["cli", "symbol", "graph"],
        "Add CLI command to dump indexed symbol graph for a repo",
    )
    assert "src/server/mcp_server.py" in probes
    assert "run_backend.py" in probes


def test_script_noise_detection():
    assert is_script_noise_path("scripts/sync_iclintz_meta.py")
    assert not is_script_noise_path("scripts/backfill_db.py")


def test_precision_top_k_demotes_extension_noise():
    paths = [
        "src/server/routers/market.py",
        "extension/src/content/injected.js",
        "extension/webpack.config.js",
        "tests/test_endpoints.py",
        "src/engine/recommendation.py",
    ]
    ordered = enforce_precision_top_k(paths, "bugfix", "Fix market stale prices", k=3)
    assert ordered[0] == "src/server/routers/market.py"
    assert "extension/src/content/injected.js" not in ordered[:3]
