"""Tests for Retrieval v6 (P0) surface-aware candidate generation."""

from brain.config.settings import settings
from brain.retrieval.pipeline import select_surface_balanced_pool
from brain.retrieval.reranker import RerankCandidate, deterministic_rerank
from brain.retrieval.types import ChannelCandidate
from brain.search.filters import surface_fusion_weight, surface_mismatch_penalty
from brain.search.surfaces import classify_surface, route_expected_surface, surfaces_for_pool
from brain.search.task_intent import derive_task_intent


def _cand(path: str, score: float) -> ChannelCandidate:
    return ChannelCandidate(channel="fused", item_id=path, raw_score=score, reranker_score=score)


# --- surface taxonomy ---------------------------------------------------------

def test_classify_surface_basic():
    assert classify_surface("src/engine/strategy_eval.py") == "engine"
    assert classify_surface("src/ml/trainer.py") == "engine"
    assert classify_surface("src/server/routers/cards.py") == "server"
    assert classify_surface("src/db/models.py") == "database"
    assert classify_surface("alembic/versions/001.py") == "database"
    assert classify_surface("extension/src/options/core.ts") == "extension"
    assert classify_surface("scripts/sync_hermes_rag.py") == "scripts"
    assert classify_surface("run_backend.py") == "scripts"
    assert classify_surface("tests/test_engine.py") == "tests"
    assert classify_surface("README.md") == "docs"
    assert classify_surface("pyproject.toml") == "config"
    assert classify_surface("docker-compose.yml") == "config"


def test_tests_surface_wins_over_location():
    # A test file under extension/test must still classify as tests.
    assert classify_surface("extension/test/e2e-stability-fixes.test.mjs") == "tests"


def test_route_expected_surface():
    assert route_expected_surface("bugfix", "Fix ability parser crash in engine") == "engine"
    assert route_expected_surface("api_contract", "Add /game/state endpoint payload") == "server"
    assert route_expected_surface("database", "Add deck_id migration") == "database"
    assert route_expected_surface("feature", "Update README onboarding docs") == "docs"
    assert route_expected_surface("cross_surface", "Wire nginx docker compose deploy") == "config"
    assert route_expected_surface("other", "general unrelated change") == ""


def test_surfaces_for_pool_always_includes_tests():
    pools = surfaces_for_pool("server")
    assert "tests" in pools
    assert "server" in pools


# --- fusion weighting ---------------------------------------------------------

def test_surface_fusion_weight_neutral_without_route():
    assert surface_fusion_weight("scripts", "") == 1.0
    assert surface_fusion_weight("tests", "engine") == 1.0  # tests never penalised


def test_surface_fusion_weight_boost_only():
    # Boost-only at fusion: routed surface lifts, off-route never penalised.
    assert surface_fusion_weight("engine", "engine") > 1.0
    assert surface_fusion_weight("extension", "engine") == 1.0  # no fusion penalty
    assert surface_fusion_weight("database", "server") >= 1.0  # neighbour light boost


# --- mismatch penalty ---------------------------------------------------------

def test_surface_mismatch_penalty_off_surface():
    assert surface_mismatch_penalty("extension/src/background.ts", "engine", wanted=False) < 0
    # neighbour and wanted cases are spared
    assert surface_mismatch_penalty("src/server/main.py", "engine", wanted=False) == 0.0
    assert surface_mismatch_penalty("extension/src/background.ts", "engine", wanted=True) == 0.0


# --- surface-balanced pool ----------------------------------------------------

def test_select_surface_balanced_pool_guarantees_quota():
    # engine dominates; one lone test file is rank-last by score.
    reranked = [_cand(f"src/engine/mod{i}.py", 1.0 - i * 0.01) for i in range(40)]
    reranked.append(_cand("tests/test_target.py", 0.05))
    pool, pools = select_surface_balanced_pool(
        reranked, pool_limit=20, expected_surface="engine", min_quota=3
    )
    paths = [c.item_id for c in pool]
    # The tail test must be pulled into the pool by the tests quota.
    assert "tests/test_target.py" in paths
    assert pools["tests"].quota == 3
    assert len(pool) <= 20


# --- reranker surface demotion ------------------------------------------------

def test_deterministic_rerank_surface_penalty_demotes_off_surface():
    pool = [
        _r("src/engine/ability_parser.py", 1.0),
        _r("extension/src/background.ts", 0.98),
    ]
    result = deterministic_rerank(
        pool,
        "Fix ability parser crash in engine simulation",
        "bugfix",
        top_k=2,
        expected_surface="engine",
    )
    assert result.top_paths[0] == "src/engine/ability_parser.py"


def test_deterministic_rerank_unchanged_without_expected_surface():
    pool = [_r("a.py", 1.0), _r("b.py", 0.9)]
    base = deterministic_rerank(pool, "generic task", "feature", top_k=2)
    surfaced = deterministic_rerank(pool, "generic task", "feature", top_k=2, expected_surface="")
    assert base.top_paths == surfaced.top_paths


# --- flag defaults ------------------------------------------------------------

def test_v6_flag_defaults_off():
    assert settings.RETRIEVAL_V6_ENABLED is False
    assert settings.RETRIEVAL_POOL_LIMIT == 100
    assert settings.RETRIEVAL_PACK_LIMIT == 10


def test_intent_exposes_surface_and_cli():
    intent = derive_task_intent("Add a CLI subcommand to run_backend entrypoint", task_type="feature")
    assert intent.wants_cli is True
    assert intent.expected_surface == "scripts"


def _r(path: str, score: float) -> RerankCandidate:
    return RerankCandidate(path=path, reranker_score=score)
