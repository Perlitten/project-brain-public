import pytest

from brain.search.path_hints import derive_path_hints
from brain.retrieval.pipeline import _should_abstain
from brain.retrieval.types import ChannelCandidate
from brain.config.settings import settings
from eval.run_golden_eval import _evaluate_question


def test_tooltip_queries_include_shared_stylesheet_and_triggers():
    hints = derive_path_hints(
        "Explain how Tip and Term tooltips are positioned and identify CSS clipping boundaries"
    )
    assert ".css" in hints
    assert "globals.css" in hints
    assert "tip" in hints
    assert "term" in hints


def test_style_hints_are_derived_for_arbitrary_component_names():
    hints = derive_path_hints("Explain how FancyPopover is clipped by the stylesheet")
    assert "fancypopover" in hints
    assert "globals.css" in hints


def test_golden_eval_scores_unified_compact_results():
    result = _evaluate_question(
        {"id": "q1", "question": "locate the handler", "expect_files": ["brain/handler.py"]},
        {"results": [{"path": "brain/handler.py", "score": 0.9}]},
        5,
    )
    assert result["rank"] == 1
    assert result["rank_any"] == 1
    assert result["hit_at_1"] is True
    assert result["mrr"] == 1.0


def test_golden_eval_rejects_unknown_response_shape():
    with pytest.raises(ValueError, match="unknown search response shape"):
        _evaluate_question(
            {"id": "q1", "question": "locate the handler", "expect_files": ["brain/handler.py"]},
            {"items": [{"path": "brain/handler.py"}]},
            5,
        )


def test_unrelated_dense_only_query_abstains_below_confidence_floor(monkeypatch):
    monkeypatch.setattr(settings, "RETRIEVAL_V2_ENABLED", True)
    candidate = ChannelCandidate(channel="vector", item_id="brain/unrelated.py", raw_score=0.1, normalized_score=0.1)
    assert _should_abstain(
        selected=[candidate.item_id],
        reranked=[candidate],
        candidates_by_channel={"vector": [candidate]},
        task_type="feature",
    )


def test_grounded_lexical_query_does_not_abstain(monkeypatch):
    monkeypatch.setattr(settings, "RETRIEVAL_V2_ENABLED", True)
    candidate = ChannelCandidate(channel="vector", item_id="brain/target.py", raw_score=0.1, normalized_score=0.1)
    lexical = ChannelCandidate(channel="lexical", item_id="brain/target.py", raw_score=1.0, normalized_score=1.0)
    assert not _should_abstain(
        selected=[candidate.item_id],
        reranked=[candidate],
        candidates_by_channel={"vector": [candidate], "lexical": [lexical]},
        task_type="feature",
    )


def test_strong_vector_evidence_survives_weak_incidental_lexical_hit(monkeypatch):
    monkeypatch.setattr(settings, "RETRIEVAL_V2_ENABLED", True)
    vector = ChannelCandidate(channel="vector", item_id="brain/indexers/file_indexer.py", raw_score=0.8, normalized_score=0.8, reranker_score=0.8)
    lexical = ChannelCandidate(channel="lexical", item_id="apps/api/main.py", raw_score=0.2, normalized_score=0.2)
    assert not _should_abstain(
        selected=[vector.item_id],
        reranked=[vector],
        candidates_by_channel={"vector": [vector], "lexical": [lexical]},
        task_type="feature",
        keywords=["chunking"],
    )


def test_weak_incidental_lexical_hit_abstains_for_unrepresented_domain(monkeypatch):
    monkeypatch.setattr(settings, "RETRIEVAL_V2_ENABLED", True)
    vector = ChannelCandidate(channel="vector", item_id="apps/api/main.py", raw_score=0.1, normalized_score=0.1, reranker_score=0.1)
    lexical = ChannelCandidate(channel="lexical", item_id="apps/api/main.py", raw_score=0.2, normalized_score=0.2)
    assert _should_abstain(
        selected=[vector.item_id],
        reranked=[vector],
        candidates_by_channel={"vector": [vector], "lexical": [lexical]},
        task_type="feature",
        keywords=["kubernetes", "mobile", "configure"],
    )


def test_legacy_unsupported_domain_uses_terms_not_score(monkeypatch):
    monkeypatch.setattr(settings, "RETRIEVAL_V2_ENABLED", False)
    vector = ChannelCandidate(channel="vector", item_id="apps/api/main.py", raw_score=0.9, normalized_score=0.9, reranker_score=0.04)
    lexical = ChannelCandidate(channel="lexical", item_id="apps/api/main.py", raw_score=1.0, normalized_score=1.0)
    assert _should_abstain(
        selected=[vector.item_id],
        reranked=[vector],
        candidates_by_channel={"vector": [vector], "lexical": [lexical]},
        task_type="feature",
        keywords=["kubernetes", "mobile", "configure"],
    )
