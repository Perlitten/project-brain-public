import json

from brain.context.budget import BudgetedPayloadBuilder
from brain.retrieval.service import RetrievalCandidate


def test_locator_contract_contains_only_targeted_navigation_data_under_transport_cap():
    candidate = RetrievalCandidate(
        path="apps/api/routers/core.py",
        symbols=["search_code_endpoint"],
        ranges=[(467, 490)],
        channel_scores={"lexical": 1.0, "symbol": 0.5},
        score=1.5,
    )
    payload = (
        BudgetedPayloadBuilder(2400, metadata={"repo": {"path": "/app", "freshness": "current"}, "degraded": []})
        .add("results", [candidate.to_locator_dict()])
        .build()
    )
    assert payload["results"][0]["ranges"] == [[467, 490]]
    assert "content" not in payload["results"][0]
    assert len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= 2400
