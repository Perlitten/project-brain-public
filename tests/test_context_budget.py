import json
import random

import pytest

from brain.context.budget import BudgetExceeded, BudgetedPayloadBuilder, ContextBudget, truncate_utf8


def _encoded_size(value: dict) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def test_truncate_utf8_is_valid_deterministic_and_never_over_budget():
    source = "alpha🙂бета" * 20
    for limit in range(0, 80):
        first = truncate_utf8(source, limit)
        second = truncate_utf8(source, limit)
        assert first == second
        assert len(first.encode("utf-8")) <= limit


def test_context_budget_records_section_and_exclusion():
    budget = ContextBudget(max_bytes=10, allocations={"code": 6})
    value = budget.add_text("code", "abcdefghijk")
    assert len(value.encode("utf-8")) <= 6
    assert budget.snapshot()["truncated"] is True
    assert budget.snapshot()["sections"]["code"] <= 6


def test_payload_builder_fails_closed_when_metadata_cannot_fit():
    with pytest.raises(BudgetExceeded):
        BudgetedPayloadBuilder(10, metadata={"required": "metadata cannot fit"}).build()


def test_payload_builder_never_exceeds_hard_cap_for_random_payloads():
    random.seed(20260802)
    for cap in range(180, 1000, 37):
        values = ["🙂" * random.randint(0, 90) for _ in range(30)]
        payload = (
            BudgetedPayloadBuilder(cap, metadata={"repo": "/app", "version": 2})
            .add("results", [{"path": f"brain/{idx}.py", "why": value} for idx, value in enumerate(values)], priority=2)
            .add("notes", "\n".join(values))
            .build()
        )
        assert _encoded_size(payload) <= cap
        assert payload["budget"]["limit"] == cap
        assert payload["budget"]["bytes"] == _encoded_size(payload)
