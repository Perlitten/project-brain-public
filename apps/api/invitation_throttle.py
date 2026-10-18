"""Sliding-window throttle for invitation acceptance, shared by every API process.

Team mode needs full storage, and full storage always runs Redis, so the
attempt log lives there: one sorted set per client address, trimmed and
checked by a single Lua script so concurrent API processes (or replicas
behind one proxy) count against the same limit. Scores are wall-clock
seconds because a monotonic clock is per process.

Lite storage has no Redis and no team (``require_team_storage`` refuses it),
so it keeps the in-process window: Lite is a single process, where that is
exact. If Redis errors or stalls, the shared throttle falls back to the
in-process window for that attempt: still a limit, only per process.

Attempts are keyed by ``client_ip``: the socket peer, unless that peer is a
proxy listed in ``BRAIN_TRUSTED_PROXY_IPS`` (the web dashboard, which calls
the API for every invitee, and any reverse proxy in front of the API). Then
X-Forwarded-For is read from the right, skipping trusted hops, and the first
address not in the list is the client, so dashboard visitors get their own
budgets while a forged header from anyone else is ignored.
"""
from __future__ import annotations

import asyncio
import ipaddress
import math
import time
import uuid
from collections import defaultdict, deque
from functools import lru_cache
from typing import Any, Callable, Deque, Dict, Protocol

from fastapi import Request

from loguru import logger

from brain.config.settings import settings

KEY_PREFIX = "brain:team:invitation-accept:"
REDIS_DEADLINE_S = 2.0

# KEYS[1] attempt log; ARGV: now (s), window (s), limit, unique member.
# Returns -1 when the attempt is admitted, else milliseconds until a slot frees.
_SLIDING_WINDOW = """
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', '(' .. (now - window))
if redis.call('ZCARD', KEYS[1]) >= limit then
  local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
  return math.max(1, math.ceil((tonumber(oldest[2]) + window - now) * 1000))
end
redis.call('ZADD', KEYS[1], now, ARGV[4])
redis.call('PEXPIRE', KEYS[1], math.ceil(window * 1000))
return -1
"""


class AcceptThrottle(Protocol):
    async def hit(self, key: str, *, limit: int, window_s: float) -> float | None:
        """Record one attempt for ``key``; ``None`` if admitted, else seconds until it would be."""


class InProcessWindow:
    """Per-process sliding window (Lite, tests, and the fallback when Redis fails)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._attempts: Dict[str, Deque[float]] = defaultdict(deque)

    async def hit(self, key: str, *, limit: int, window_s: float) -> float | None:
        now = self._clock()
        for stale in [k for k, w in self._attempts.items() if not w or w[-1] < now - window_s]:
            del self._attempts[stale]
        window = self._attempts[key]
        while window and window[0] < now - window_s:
            window.popleft()
        if len(window) >= limit:
            return max(window[0] + window_s - now, 0.001)
        window.append(now)
        return None


class RedisWindow:
    """Sliding window kept in Redis, so every API process shares one limit per key."""

    def __init__(self, redis: Any, *, fallback: AcceptThrottle | None = None,
                 clock: Callable[[], float] = time.time, prefix: str = KEY_PREFIX,
                 deadline_s: float = REDIS_DEADLINE_S) -> None:
        self._redis = redis
        self._fallback = fallback or InProcessWindow()
        self._clock = clock
        self._prefix = prefix
        self._deadline_s = deadline_s

    async def hit(self, key: str, *, limit: int, window_s: float) -> float | None:
        try:
            result = await asyncio.wait_for(
                self._redis.eval(_SLIDING_WINDOW, 1, self._prefix + key, repr(self._clock()), repr(float(window_s)),
                                 str(limit), uuid.uuid4().hex),
                timeout=self._deadline_s,
            )
        except Exception as exc:
            logger.warning("Invitation throttle store unavailable ({}); using the per-process limit",
                           type(exc).__name__)
            return await self._fallback.hit(key, limit=limit, window_s=window_s)
        retry_ms = int(result)
        return None if retry_ms < 0 else retry_ms / 1000


def default_throttle() -> AcceptThrottle:
    """Shared Redis window under full storage; the in-process window under Lite."""
    if settings.BRAIN_STORAGE_MODE == "lite":
        return InProcessWindow()
    from brain.database.session import redis_client

    return RedisWindow(redis_client)


Network = ipaddress.IPv4Network | ipaddress.IPv6Network


@lru_cache(maxsize=4)
def _trusted_proxies(raw: str) -> tuple[Network, ...]:
    networks = []
    for entry in (part.strip() for part in raw.split(",")):
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            logger.warning("Ignoring BRAIN_TRUSTED_PROXY_IPS entry {!r}: not an IP address or CIDR", entry)
    return tuple(networks)


def _address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value.strip())
    except ValueError:
        return None


def _trusted(address: ipaddress.IPv4Address | ipaddress.IPv6Address | None, proxies: tuple[Network, ...]) -> bool:
    return address is not None and any(address.version == net.version and address in net for net in proxies)


def client_ip(request: Request) -> str:
    """The throttle key: the socket peer, or the client a trusted proxy forwarded for (see the module doc)."""
    peer = request.client.host if request.client and request.client.host else "unknown"
    proxies = _trusted_proxies(settings.BRAIN_TRUSTED_PROXY_IPS)
    if not proxies or not _trusted(_address(peer), proxies):
        return peer
    hops = [hop for header in request.headers.getlist("x-forwarded-for") for hop in header.split(",")]
    for hop in reversed(hops):
        address = _address(hop)
        if address is None:
            break  # a hop a trusted proxy could not vouch for: count the request against the proxy
        if not _trusted(address, proxies):
            return str(address)
    return peer


def retry_after_header(seconds: float) -> str:
    """``Retry-After`` value: whole seconds, never zero."""
    return str(max(1, math.ceil(seconds)))
