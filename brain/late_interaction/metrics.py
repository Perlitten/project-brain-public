"""Process-local telemetry for the optional late-interaction path."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Any


@dataclass
class _Metrics:
    provider_requests: int = 0
    provider_failures: int = 0
    provider_latency_ms_total: float = 0.0
    dual_write_successes: int = 0
    dual_write_failures: int = 0
    rerank_requests: int = 0
    rerank_applied: int = 0
    rerank_fail_open: int = 0
    rerank_timeouts: int = 0
    rerank_latency_ms_total: float = 0.0


_metrics = _Metrics()
_lock = Lock()
_started_at = datetime.now(timezone.utc)
_provider_latency_samples: deque[float] = deque(maxlen=512)
_rerank_latency_samples: deque[float] = deque(maxlen=512)


def increment(name: str, value: int = 1) -> None:
    with _lock:
        setattr(_metrics, name, int(getattr(_metrics, name)) + value)


def observe(name: str, value: float) -> None:
    with _lock:
        numeric = float(value)
        setattr(_metrics, name, float(getattr(_metrics, name)) + numeric)
        if math.isfinite(numeric):
            if name == "provider_latency_ms_total":
                _provider_latency_samples.append(numeric)
            elif name == "rerank_latency_ms_total":
                _rerank_latency_samples.append(numeric)


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + ((ordered[upper] - ordered[lower]) * fraction)


def snapshot() -> dict[str, Any]:
    with _lock:
        payload = asdict(_metrics)
        provider_samples = list(_provider_latency_samples)
        rerank_samples = list(_rerank_latency_samples)
    requests = int(payload["provider_requests"])
    reranks = int(payload["rerank_requests"])
    payload["provider_latency_ms_avg"] = (
        round(float(payload["provider_latency_ms_total"]) / requests, 2) if requests else 0.0
    )
    payload["rerank_latency_ms_avg"] = (
        round(float(payload["rerank_latency_ms_total"]) / reranks, 2) if reranks else 0.0
    )
    payload["provider_latency_ms_p50"] = round(
        _percentile(provider_samples, 0.50),
        2,
    )
    payload["provider_latency_ms_p95"] = round(
        _percentile(provider_samples, 0.95),
        2,
    )
    payload["provider_latency_sample_count"] = len(provider_samples)
    payload["rerank_latency_ms_p50"] = round(
        _percentile(rerank_samples, 0.50),
        2,
    )
    payload["rerank_latency_ms_p95"] = round(
        _percentile(rerank_samples, 0.95),
        2,
    )
    payload["rerank_latency_sample_count"] = len(rerank_samples)
    payload["observed_since"] = _started_at.isoformat()
    payload["window"] = "current API process; last 512 latency samples"
    payload["durable"] = False
    return payload


def reset_for_tests() -> None:
    global _metrics, _started_at
    with _lock:
        _metrics = _Metrics()
        _provider_latency_samples.clear()
        _rerank_latency_samples.clear()
        _started_at = datetime.now(timezone.utc)
