"""Tests for Retrieval v6 P2 two-stage semantic rerank."""

import asyncio
import pytest

from brain.retrieval.reranker import (
    RERANKER_VERSION,
    RerankCandidate,
    deterministic_rerank,
    rerank_top_k,
    semantic_rerank,
)


def _c(path, score, vec=0.0, sym=0.0, lex=0.0, graph=0.0, summary=""):
    return RerankCandidate(
        path=path, reranker_score=score, vector_score=vec, symbol_score=sym,
        lexical_score=lex, graph_score=graph, summary=summary,
    )


def test_semantic_rerank_promotes_strong_pool_reachable_file():
    # A strong server file sits at rank ~12 by raw fusion; conservative v5 keeps
    # pool[:10]. Semantic rescore should lift it on surface + vector + summary signals.
    pool = [_c(f"src/server/filler{i}.py", 1.0 - i * 0.01) for i in range(11)]
    target = _c(
        "src/server/routers/ml.py", 0.80,
        vec=0.9, sym=1.0, summary="ml simulate endpoint route handler for simulation",
    )
    pool.append(target)  # index 11 -> outside conservative top-10
    result = semantic_rerank(
        pool, "Add ml simulate endpoint route handler", "feature",
        top_k=10, expected_surface="server",
    )
    assert "src/server/routers/ml.py" in result.top_paths
    assert result.stage == "semantic"
    # promotion is recorded
    promoted = [c for c in result.candidates if "promoted by semantic rescore" in c.explanation]
    assert any(c.path == "src/server/routers/ml.py" for c in promoted)


def test_semantic_rerank_sets_confidence_and_excluded():
    pool = [_c(f"f{i}.py", 1.0 - i * 0.02, vec=0.5) for i in range(20)]
    result = semantic_rerank(pool, "generic feature task", "feature", top_k=10)
    assert all(0.0 <= c.confidence <= 1.0 for c in result.candidates)
    assert len(result.top_paths) == 10
    # excluded_relevant lists near-cutoff candidates
    assert isinstance(result.excluded_relevant, list)


def test_two_stage_rerank_uses_semantic_and_sets_fallback_reason():
    pool = [_c(f"src/engine/m{i}.py", 1.0 - i * 0.01, vec=0.4) for i in range(40)]
    result = asyncio.run(rerank_top_k(
        pool, "fix engine bug", "bugfix", repo_hash="r", top_k=10,
        two_stage=True, commit_hash="abc123def", use_cache=False, expected_surface="engine",
    ))
    assert result.stage == "semantic"
    assert result.fallback_reason == "llm_disabled"
    assert result.reranker_version.endswith("_2s")
    assert len(result.top_paths) == 10


def test_v5_path_unchanged_when_not_two_stage():
    pool = [_c(f"a{i}.py", 1.0 - i * 0.05) for i in range(15)]
    det = deterministic_rerank(pool, "generic", "feature", top_k=10)
    one_stage = asyncio.run(rerank_top_k(
        pool, "generic", "feature", repo_hash="r", top_k=10, two_stage=False, use_cache=False,
    ))
    assert one_stage.stage == "deterministic"
    assert one_stage.top_paths == det.top_paths
    assert one_stage.reranker_version.endswith("_1s")


def test_cache_key_namespaced_by_version_and_stage():
    pool = [_c(f"a{i}.py", 1.0 - i * 0.05) for i in range(12)]
    r1 = asyncio.run(rerank_top_k(pool, "t", "feature", repo_hash="r", two_stage=True, use_cache=False))
    r2 = asyncio.run(rerank_top_k(pool, "t", "feature", repo_hash="r", two_stage=False, use_cache=False))
    assert r1.cache_key != r2.cache_key
    assert RERANKER_VERSION in r1.cache_key


@pytest.mark.parametrize("two_stage", [False, True])
def test_snapshot_cache_binds_full_source_revision(monkeypatch, two_stage):
    cached = {}
    monkeypatch.setattr("brain.retrieval.reranker._load_cache", cached.get)
    monkeypatch.setattr("brain.retrieval.reranker._save_cache", lambda key, result: cached.__setitem__(key, result))
    revisions = ["snapshot:" + "a" * 40 + ":" + "b" * 64,
                 "snapshot:" + "c" * 40 + ":" + "b" * 64,
                 "snapshot:" + "c" * 40 + ":" + "d" * 64]
    pool = [_c("src/first.py", 0.9), _c("src/second.py", 0.8)]
    keys = []
    for revision in revisions:
        result = asyncio.run(rerank_top_k(pool, "task", "feature", repo_hash="r",
                                         commit_hash=revision, two_stage=two_stage))
        assert not result.cache_hit
        keys.append(result.cache_key)
    assert len(set(keys)) == len(revisions)
    repeat = asyncio.run(rerank_top_k(pool, "task", "feature", repo_hash="r",
                                     commit_hash=revisions[-1], two_stage=two_stage))
    assert repeat.cache_hit
    assert repeat.cache_key == keys[-1]
