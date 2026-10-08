from types import SimpleNamespace

from brain.memory.harness_store import _task_status_filter
from brain.retrieval.pipeline import _lexical_file_candidate, _should_abstain
from brain.retrieval.types import ChannelCandidate


def test_real_file_summary_candidate_prevents_false_abstention():
    file_row = SimpleNamespace(path="brain/auth.py", summary="authentication middleware")
    candidate = _lexical_file_candidate(file_row, 1)
    assert candidate.metadata["summary"] == "authentication middleware"
    assert not _should_abstain(
        selected=[candidate.item_id],
        reranked=[ChannelCandidate("vector", candidate.item_id, 0.1, normalized_score=0.1)],
        candidates_by_channel={"lexical": [candidate]},
        task_type="feature",
        keywords=["authentication"],
    )


def test_running_status_predicate_contains_every_display_running_state():
    expression = str(_task_status_filter("__running__").compile(compile_kwargs={"literal_binds": True}))
    for status in ("running", "in_progress", "started", "indexing", "processing", "claimed", "artifacted", "memory_updated", "validating", "acceptance_pending"):
        assert status in expression


def test_queued_status_predicate_matches_ui_queued_group():
    expression = str(_task_status_filter("__queued__").compile(compile_kwargs={"literal_binds": True}))
    for status in ("queued", "pending", "created", "routed", "scheduled", "waiting"):
        assert status in expression
