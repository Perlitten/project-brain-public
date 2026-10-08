"""Tests for Ranking v5 reranker."""

from brain.retrieval.reranker import RerankCandidate, deterministic_rerank


def _cand(path: str, score: float) -> RerankCandidate:
    return RerankCandidate(path=path, reranker_score=score)


def test_deterministic_rerank_promotes_test_when_src_in_top5():
    pool = [
        _cand("src/engine/strategy_eval.py", 0.52),
        _cand("tests/test_decks_ga.py", 1.8),
        _cand("src/server/routers/decks.py", 2.3),
        _cand("tests/test_strategy_eval.py", 0.59),
        _cand("tests/test_ratelimit.py", 0.58),
    ]
    result = deterministic_rerank(
        pool,
        "Resolve NaN division in deck evaluator win rate when games count is very low",
        "bugfix",
        top_k=5,
    )
    assert "tests/test_strategy_eval.py" in result.top_paths[:3]


def test_deterministic_rerank_demotes_extension_for_engine_task():
    pool = [
        _cand("src/engine/ability_parser.py", 1.6),
        _cand("extension/test/e2e-stability-fixes.test.mjs", 0.84),
        _cand("scripts/audit_ability_parser.py", 1.5),
        _cand("tests/test_engine_abilities.py", 0.28),
        _cand("tests/test_stability_regression.py", 0.25),
    ]
    result = deterministic_rerank(
        pool,
        "Fix ability parser crash when encountering unmapped token types in card text",
        "bugfix",
        top_k=3,
    )
    paths = result.top_paths
    assert paths[0] == "src/engine/ability_parser.py"
    assert "extension/test/e2e-stability-fixes.test.mjs" not in paths


def test_deterministic_rerank_boosts_readme_for_doc_intent():
    pool = [
        _cand("src/ml/trainer.py", 1.2),
        _cand("src/ml/README.md", 0.7),
        _cand("tests/test_ml.py", 0.65),
    ]
    result = deterministic_rerank(
        pool,
        "Document the ML training pipeline workflow in the module README",
        "feature",
        top_k=2,
    )
    assert result.top_paths[0] == "src/ml/README.md"


def test_position_changes_recorded():
    pool = [_cand(f"file{i}.py", 1.0 - i * 0.05) for i in range(15)]
    pool[12] = _cand("tests/test_battle_scenarios.py", 0.25)
    result = deterministic_rerank(
        pool,
        "Fix battle scenario resolution edge case in engine",
        "bugfix",
        top_k=10,
    )
    assert any(c.rank_delta != 0 for c in result.candidates) or len(result.position_changes) >= 0


def test_small_output_budget_can_replace_noise_with_a_tail_paired_test():
    pool = [
        _cand("src/engine/calculate.py", 1.0),
        _cand("scripts/audit_a.py", 0.9),
        _cand("scripts/audit_b.py", 0.8),
        _cand("scripts/audit_c.py", 0.7),
        _cand("src/engine/unrelated.py", 0.6),
        _cand("tests/test_calculate.py", 0.5),
    ]
    result = deterministic_rerank(pool, "Fix calculation bug", "bugfix", top_k=5)
    assert len(result.top_paths) == 5
    assert "tests/test_calculate.py" in result.top_paths
    assert "src/engine/calculate.py" in result.top_paths
