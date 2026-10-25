"""Shared time helpers for ``brain.team`` internals.

Three datetime patterns repeated across the package:

* ``_now`` — current time, always timezone-aware UTC.
* ``_iso`` — ``datetime.isoformat()`` or ``None`` for absent values.
* ``_aware`` — normalize a value to aware UTC (naive is assumed UTC).

``registry.py`` keeps its own ``_now`` (a ``str`` for file payloads) and
``restore_fence.py`` its own ``_iso`` (naive-aware); those differ in return
type/contract and stay local.
"""
from __future__ import annotations

from datetime import datetime, timezone


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
