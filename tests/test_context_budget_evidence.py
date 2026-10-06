"""A transport budget must retain usable evidence, not just navigation."""
import json

from brain.context.budget import BudgetedPayloadBuilder


def test_tail_envelope_overhead_preserves_admitted_code_prefix():
    slices = [
        {"path": "brain/search/code_search.py", "range": [i, i + 10],
         "content": "def query():\n    return value\n" * i}
        for i in range(5, 27)
    ]
    candidates = [
        {"path": f"brain/file_{i}.py", "symbols": ["method"], "ranges": [[1, 42]]}
        for i in range(6)
    ]
    for cap in range(2200, 14001, 10):
        payload = (
            BudgetedPayloadBuilder(cap, metadata={
                "status": "ok", "repo": {"path": "/app", "freshness": "current"}})
            .add("candidates", candidates, priority=4)
            .add("slices", slices, priority=3)
            .add("memory", {"rules": [], "decisions": []}, priority=1)
            .build()
        )
        assert payload.get("slices"), f"lost all code at byte cap {cap}"
        assert payload["slices"] == slices[:len(payload["slices"])]
        size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())
        assert size <= cap
        assert payload["budget"]["bytes"] == size
