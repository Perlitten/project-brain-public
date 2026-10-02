import json

from apps.api.schemas import ContextRequest, SearchRequest
from brain.context.contracts import bounded_payload


def test_compact_search_schema_defaults_to_locator_without_debug():
    request = SearchRequest(query="find search_code")
    assert request.max_tokens == 600
    assert request.response_mode == "locator"
    assert request.include_debug is False


def test_runtime_context_is_ephemeral_by_default_and_hard_capped():
    request = ContextRequest(task_description="fix a retrieval bug")
    assert request.persist is False
    assert request.max_tokens == 3500

    payload = bounded_payload(
        500,
        {"repo": {"path": "/app"}},
        slices=[{"path": "brain/search/code_search.py", "content": "x" * 1000}],
    )
    assert len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= 500
