import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import brain.search.local_lexical as module


def test_stopped_ranking_does_not_parse_the_corpus(monkeypatch):
    stop = threading.Event()
    stop.set()
    tokenize = Mock(side_effect=AssertionError("cancelled corpus was parsed"))
    monkeypatch.setattr(module, "_tokens", tokenize)
    assert module.rank_chunks([("src/core.py", 1, 2, "core implementation")], "core", 5, stop=stop) == []
    tokenize.assert_not_called()


@pytest.mark.asyncio
async def test_lexical_cancellation_signals_cpu_worker(monkeypatch):
    async def rows():
        yield "src/core.py", 1, 2, "core implementation"

    session = SimpleNamespace(stream=AsyncMock(return_value=rows()))
    class SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(module, "async_session_factory", SessionContext)
    started, stopped = threading.Event(), threading.Event()

    def rank(rows, query, budget, *, stop):
        started.set()
        assert stop.wait(1)
        stopped.set()
        return []

    monkeypatch.setattr(module, "rank_chunks", rank)
    request = asyncio.create_task(module.local_lexical(1, "core", 5))
    assert await asyncio.to_thread(started.wait, 1)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert await asyncio.to_thread(stopped.wait, 1)
