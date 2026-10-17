"""Invitation acceptance throttle: one sliding window shared by every API process.

The shared store is the CI Redis service when it answers; locally, fakeredis
(with Lua) stands in when installed; otherwise the shared-store tests skip.
"""
import asyncio
import uuid
from contextlib import asynccontextmanager

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from apps.api import invitation_throttle
from apps.api.invitation_throttle import InProcessWindow, RedisWindow, default_throttle
from apps.api.routers import team_invitations
from brain.config.settings import settings
from brain.team import invitations


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest_asyncio.fixture
async def shared_store():
    """A factory of independent Redis clients that all reach one server: one client per "process"."""
    clients: list = []
    prefix = f"test:invitation-accept:{uuid.uuid4().hex}:"
    try:
        from redis.asyncio import Redis

        probe = Redis.from_url(settings.REDIS_URL or "redis://127.0.0.1:6379/0", socket_connect_timeout=1,
                               socket_timeout=2)
        await probe.ping()
        await probe.aclose()

        def connect():
            return Redis.from_url(settings.REDIS_URL or "redis://127.0.0.1:6379/0", decode_responses=True)
    except Exception:
        try:
            import fakeredis
            import lupa  # noqa: F401 - fakeredis needs it for EVAL
        except ImportError:
            pytest.skip("No Redis server and no fakeredis[lua] to stand in for the shared store")
        server = fakeredis.FakeServer()

        def connect():
            return fakeredis.FakeAsyncRedis(server=server, decode_responses=True)

    def new_client():
        client = connect()
        clients.append(client)
        return client

    yield new_client, prefix
    if clients:
        keys = [key async for key in clients[0].scan_iter(match=prefix + "*")]
        if keys:
            await clients[0].delete(*keys)
    for client in clients:
        await client.aclose()


@pytest.mark.asyncio
async def test_in_process_window_limits_and_reports_when_a_slot_frees():
    clock = Clock()
    window = InProcessWindow(clock=clock)
    for _ in range(3):
        assert await window.hit("10.0.0.1", limit=3, window_s=300) is None
        clock.now += 10
    assert await window.hit("10.0.0.1", limit=3, window_s=300) == pytest.approx(270)
    # A refused attempt is not recorded, and other addresses keep their own window.
    assert await window.hit("10.0.0.2", limit=3, window_s=300) is None
    clock.now += 271
    assert await window.hit("10.0.0.1", limit=3, window_s=300) is None


@pytest.mark.asyncio
async def test_two_processes_share_one_redis_window(shared_store):
    new_client, prefix = shared_store
    clock = Clock()
    # Two limiter objects with their own connections, as two API processes would have.
    first = RedisWindow(new_client(), clock=clock, prefix=prefix)
    second = RedisWindow(new_client(), clock=clock, prefix=prefix)
    assert await first.hit("10.0.0.1", limit=3, window_s=300) is None
    clock.now += 5
    assert await second.hit("10.0.0.1", limit=3, window_s=300) is None
    clock.now += 5
    assert await first.hit("10.0.0.1", limit=3, window_s=300) is None
    clock.now += 5
    for limiter in (first, second):
        assert await limiter.hit("10.0.0.1", limit=3, window_s=300) == pytest.approx(285)
    assert await second.hit("10.0.0.2", limit=3, window_s=300) is None
    # The oldest attempt leaves the window: exactly one slot frees, shared by both.
    clock.now += 286
    assert await second.hit("10.0.0.1", limit=3, window_s=300) is None
    assert await first.hit("10.0.0.1", limit=3, window_s=300) is not None


@pytest.mark.asyncio
async def test_concurrent_attempts_across_processes_never_exceed_the_limit(shared_store):
    new_client, prefix = shared_store
    limiters = [RedisWindow(new_client(), prefix=prefix) for _ in range(4)]
    results = await asyncio.gather(*(limiters[i % 4].hit("10.0.0.9", limit=5, window_s=300) for i in range(20)))
    assert sum(result is None for result in results) == 5


class _BrokenRedis:
    def __init__(self, stall: bool = False) -> None:
        self.stall = stall

    async def eval(self, *args):
        if self.stall:
            await asyncio.sleep(5)
        raise ConnectionError("synthetic: Redis unavailable")


@pytest.mark.asyncio
@pytest.mark.parametrize("stall", [False, True])
async def test_redis_failure_falls_back_to_the_per_process_window(stall):
    limiter = RedisWindow(_BrokenRedis(stall), fallback=InProcessWindow(), deadline_s=0.05)
    assert await limiter.hit("10.0.0.1", limit=2, window_s=300) is None
    assert await limiter.hit("10.0.0.1", limit=2, window_s=300) is None
    assert await limiter.hit("10.0.0.1", limit=2, window_s=300) is not None


def test_lite_keeps_the_in_process_window_and_full_storage_shares_redis(monkeypatch):
    monkeypatch.setattr(settings, "BRAIN_STORAGE_MODE", "lite")
    assert isinstance(default_throttle(), InProcessWindow)
    monkeypatch.setattr(settings, "BRAIN_STORAGE_MODE", "full")
    assert isinstance(default_throttle(), RedisWindow)


def test_retry_after_is_whole_seconds_and_never_zero():
    assert invitation_throttle.retry_after_header(0.001) == "1"
    assert invitation_throttle.retry_after_header(284.2) == "285"


def _accept_app(throttle) -> FastAPI:
    app = FastAPI()
    app.state.invitation_throttle = throttle
    app.include_router(team_invitations.router)
    return app


@pytest.mark.asyncio
async def test_two_api_apps_share_the_accept_limit_and_answer_429_with_retry_after(shared_store, monkeypatch):
    new_client, prefix = shared_store
    monkeypatch.setattr(settings, "BRAIN_STORAGE_MODE", "full")
    monkeypatch.setattr(team_invitations, "ACCEPT_LIMIT", 3)

    @asynccontextmanager
    async def no_database():
        class _Session:
            @asynccontextmanager
            async def begin(self):
                yield

        yield _Session()

    async def reject(session, token):
        raise invitations.InvitationRejected()

    monkeypatch.setattr(team_invitations, "async_session_factory", no_database)
    monkeypatch.setattr(team_invitations.invitations, "accept_invitation", reject)
    apps = [_accept_app(RedisWindow(new_client(), prefix=prefix)) for _ in range(2)]
    clients = [httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") for app in apps]
    try:
        body = {"token": "pbi_" + "y" * 43}
        for client in (clients[0], clients[1], clients[0]):
            assert (await client.post("/team/invitations/accept", json=body)).status_code == 403
        for client in clients:
            throttled = await client.post("/team/invitations/accept", json=body)
            assert throttled.status_code == 429, throttled.text
            assert 1 <= int(throttled.headers["Retry-After"]) <= team_invitations.ACCEPT_WINDOW_S
    finally:
        for client in clients:
            await client.aclose()
