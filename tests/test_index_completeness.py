"""B3 — index completeness states: a run is `completed` only when every
scanned file reached a consistent end-state in all required stores; partial
results are `degraded` and must not satisfy complete-exact-commit contracts.
"""
from brain.indexers.file_indexer import compute_run_status


def test_clean_run_is_completed():
    assert (
        compute_run_status(processed=3, unchanged=10, failed=0, graph_failed=0, graph_error=False)
        == "completed"
    )


def test_empty_repo_is_completed():
    assert (
        compute_run_status(processed=0, unchanged=0, failed=0, graph_failed=0, graph_error=False)
        == "completed"
    )


def test_single_failed_file_degrades_run():
    assert (
        compute_run_status(processed=5, unchanged=0, failed=1, graph_failed=0, graph_error=False)
        == "degraded"
    )


def test_only_unchanged_plus_failure_is_degraded_not_failed():
    # Files already indexed are usable data — the run is partially valid.
    assert (
        compute_run_status(processed=0, unchanged=4, failed=1, graph_failed=0, graph_error=False)
        == "degraded"
    )


def test_everything_failed_is_failed():
    assert (
        compute_run_status(processed=0, unchanged=0, failed=7, graph_failed=0, graph_error=False)
        == "failed"
    )


def test_per_file_graph_failure_degrades_run():
    # SQL side committed but the Neo4j per-file update failed — a required
    # store is inconsistent, so the run cannot be completed.
    assert (
        compute_run_status(processed=0, unchanged=0, failed=0, graph_failed=3, graph_error=False)
        == "degraded"
    )


def test_graph_post_process_error_degrades_run():
    assert (
        compute_run_status(processed=4, unchanged=0, failed=0, graph_failed=0, graph_error=True)
        == "degraded"
    )


def test_degraded_never_returned_as_completed():
    # Whatever the counter mix, any failure or graph gap must not satisfy a
    # contract that requires complete exact-commit data.
    assert (
        compute_run_status(processed=100, unchanged=50, failed=1, graph_failed=1, graph_error=True)
        != "completed"
    )
