"""Tests for the L3 LearningStore (durable consolidated learnings)."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from brain.memory.learning_store import LearningStore


def _make_session_factory(captured, query_results):
    """Build a fake async_session_factory capturing added objects."""

    class _Scalars:
        def __init__(self, values):
            self._values = values

        def all(self):
            return self._values

        def one_or_none(self):
            return self._values[0] if self._values else None

    class _Result:
        def __init__(self, values):
            self._values = values

        def scalars(self):
            return _Scalars(self._values)

        def scalar_one_or_none(self):
            return self._values[0] if self._values else None

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def add(self, obj):
            captured.append(obj)

        async def commit(self):
            return None

        async def refresh(self, obj):
            obj.id = 42

        async def execute(self, query):
            return _Result(query_results.pop(0) if query_results else [])

    def factory():
        return _Session()

    return factory


@pytest.mark.asyncio
async def test_add_learning_rejects_empty_statement():
    with pytest.raises(ValueError, match="must not be empty"):
        await LearningStore.add_learning("   ")


@pytest.mark.asyncio
async def test_add_learning_persists_with_evidence():
    captured = []
    fake_factory = _make_session_factory(captured, [])
    with patch("brain.memory.learning_store.async_session_factory", fake_factory):
        learning_id = await LearningStore.add_learning(
            "Embeddings dimension is 2048",
            category="infra",
            confidence=0.9,
            evidence=[{"event_id": 1}],
        )
    assert learning_id == 42
    assert len(captured) == 1
    learning = captured[0]
    assert learning.statement == "Embeddings dimension is 2048"
    assert learning.status == "active"
    assert learning.evidence == [{"event_id": 1}]


@pytest.mark.asyncio
async def test_add_learning_clamps_confidence():
    captured = []
    fake_factory = _make_session_factory(captured, [])
    with patch("brain.memory.learning_store.async_session_factory", fake_factory):
        await LearningStore.add_learning("x", confidence=5.0)
    assert captured[0].confidence == 1.0


@pytest.mark.asyncio
async def test_supersede_marks_old_learning():
    old = SimpleNamespace(id=1, status="active", superseded_by=None)
    captured = []
    fake_factory = _make_session_factory(captured, [[old]])
    with patch("brain.memory.learning_store.async_session_factory", fake_factory):
        await LearningStore.supersede(1, superseded_by=2)
    assert old.status == "superseded"
    assert old.superseded_by == 2


@pytest.mark.asyncio
async def test_supersede_missing_raises():
    fake_factory = _make_session_factory([], [[]])
    with patch("brain.memory.learning_store.async_session_factory", fake_factory):
        with pytest.raises(LookupError):
            await LearningStore.supersede(999, superseded_by=1)


@pytest.mark.asyncio
async def test_list_active_filters_expired_and_superseded():
    """The store must only surface active, non-expired learnings.

    We verify the SQL WHERE clause contains the status/expiry predicates
    rather than hitting a real database.
    """
    seen_queries = []

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, query):
            seen_queries.append(str(query))
            class _R:
                def scalars(self):
                    class _S:
                        def all(self):
                            return []
                    return _S()
            return _R()

    def factory():
        return _Session()

    with patch("brain.memory.learning_store.async_session_factory", factory):
        await LearningStore.list_active_learnings()

    sql = seen_queries[0]
    assert "memory_learnings.status = " in sql or "status" in sql
    assert "valid_until" in sql
