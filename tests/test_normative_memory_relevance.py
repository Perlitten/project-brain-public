from types import SimpleNamespace

import pytest

from brain.memory.relevance import select_relevant_normative_memory
from scripts.classify_normative_memory import classify


def test_memory_classifier_preserves_normative_decisions_and_marks_activity_history():
    assert classify("ADR: repository scope is an invariant") == "normative"
    assert classify("Full reindex complete", "verified all repositories") == "operational"
    assert classify("Audit checkpoint", "verified deployment") == "audit_checkpoint"


@pytest.mark.asyncio
async def test_runtime_relevance_requires_task_or_path_overlap_for_repo_scoped_rules(monkeypatch):
    irrelevant_critical = SimpleNamespace(
        id=1,
        name="Production release policy",
        description="Only release coordinators may rotate deployment credentials.",
        applies_to={"paths": ["deploy/credentials.py"]},
        repo_path="/app",
        severity="critical",
    )
    relevant_rule = SimpleNamespace(
        id=2,
        name="Search API contract",
        description="Locator requests must preserve a bounded result schema.",
        applies_to={"paths": ["apps/api/routers/core.py"]},
        repo_path="/app",
        severity="low",
    )
    relevant_decision = SimpleNamespace(
        id=3,
        title="Search response is evidence only",
        description="Keep the search API response bounded.",
        reason="Avoid oversized locator payloads.",
        affected_files=["apps/api/routers/core.py"],
        affected_modules=[],
        repo_path="/app",
    )

    class _Scalars:
        def __init__(self, values):
            self.values = values

        def all(self):
            return self.values

    class _Result:
        def __init__(self, values):
            self.values = values

        def scalars(self):
            return _Scalars(self.values)

    class _Session:
        def __init__(self):
            self.calls = 0

        async def execute(self, _statement):
            self.calls += 1
            return _Result(
                [irrelevant_critical, relevant_rule]
                if self.calls == 1
                else [relevant_decision]
            )

    class _Factory:
        async def __aenter__(self):
            return _Session()

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr("brain.memory.relevance.async_session_factory", lambda: _Factory())
    selected = await select_relevant_normative_memory(
        "Update the bounded search API response",
        ["apps/api/routers/core.py"],
        "/app",
    )

    assert [rule["id"] for rule in selected["rules"]] == [2]
    assert [decision["id"] for decision in selected["decisions"]] == [3]
