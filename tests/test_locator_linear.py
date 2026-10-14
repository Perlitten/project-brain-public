"""Ranking invariants independent of the curated benchmark answers."""
import pytest

from brain.config.settings import Settings, settings
from brain.retrieval.locator_fusion import merge_linear
from brain.retrieval.service import RetrievalCandidate, RetrievalService
from brain.search.filters import query_file_type_weight


@pytest.fixture(autouse=True)
def linear_policy(monkeypatch):
    monkeypatch.setattr(settings, "RETRIEVAL_V2_ENABLED", True)
    monkeypatch.setattr(settings, "DEFAULT_EMBEDDING_PROVIDER", "mock")


def test_new_install_defaults_select_bounded_linear_without_reranking():
    cfg = Settings(_env_file=None)
    assert cfg.BRAIN_AGENT_CONTEXT_V2_MODE == "on"
    assert cfg.RETRIEVAL_V2_ENABLED and cfg.RETRIEVAL_V2_FUSION == "linear"
    assert not cfg.RETRIEVAL_V2_RERANK_ENABLED


def test_strong_dense_hit_survives_noisy_side_channels():
    dense = RetrievalCandidate("src/engine.py", channel_scores={"vector":0.96})
    noisy = RetrievalCandidate("src/utility.py", channel_scores={"vector":0.05,"lexical":1,"symbol":1})
    assert merge_linear([dense,noisy], [], "Locate the implementation", 1)[0].path == dense.path


def test_duplicate_chunks_and_symbols_cannot_accumulate_rank():
    raw = {"chunks":[{"file_path":"src/one.py","similarity":0.95},
                     *[{"file_path":"src/many.py","similarity":0.1} for _ in range(50)]],
           "symbols":[{"path":"src/many.py","name":"helper"} for _ in range(50)]}
    candidates = RetrievalService._normalize(raw, 10)
    assert candidates[0].path == "src/one.py"


def test_single_dense_candidate_has_positive_score_and_zero_stays_zero():
    candidates = RetrievalService._normalize({"chunks":[{"file_path":"src/a.py","similarity":0.9}]}, 10)
    assert candidates[0].score == 1
    candidates = RetrievalService._normalize({"chunks":[{"file_path":"src/a.py","similarity":0}]}, 10)
    assert candidates[0].channel_scores["vector"] == 0


@pytest.mark.parametrize("query,first", [("Where is compression implemented?","src/compression.py"),
                                         ("Which tests cover compression?","tests/test_compression.py")])
def test_test_demotion_depends_on_query(query,first):
    candidates = [RetrievalCandidate("src/compression.py",channel_scores={"vector":0.8}),
                  RetrievalCandidate("tests/test_compression.py",channel_scores={"vector":1})]
    assert merge_linear(candidates, [], query, 2)[0].path == first
    if "tests" in query:
        assert query_file_type_weight("test",query,"tests/test_compression.py") == 1


def test_explicit_named_symbol_can_win_without_basename_guesses():
    candidates = [RetrievalCandidate("src/a.py",channel_scores={"vector":1}),
                  RetrievalCandidate("src/b.py",channel_scores={"vector":0.9,"identifier":1})]
    assert merge_linear(candidates, [], "Find BillingManager", 1)[0].path == "src/b.py"


def test_offline_lexical_preserves_frequency_and_ignores_unmatched_text():
    from brain.search.local_lexical import rank_chunks
    rows = [("src/decoder.py",1,4,"def decode_archive(data): return decompress(data)"),
            ("src/sorter.py",1,4,"def stable_sort(items): return sorted(items)")]
    assert rank_chunks(rows,"decode_archive",2)[0]["path"] == "src/decoder.py"
