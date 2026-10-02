"""Shared, fail-open late-interaction application path for retrieval surfaces.

This module owns feature gating, deterministic canary sampling, the remote client
call, and durable shadow recording.  Surface-specific ranking stays in the
caller-provided counterfactual builder so file and chunk contracts do not leak
into one another.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Sequence

from loguru import logger

from brain.config.settings import settings


CounterfactualBuilder = Callable[[Any], Awaitable[Sequence[str]] | Sequence[str]]
LateInteractionMode = Literal["default", "deep"]


def _positive_int_setting(name: str, default: int) -> int:
    try:
        value = int(getattr(settings, name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def late_interaction_candidate_budget() -> int:
    """Return the remote chunk-candidate cap (legacy path limit is unrelated)."""
    limits = [
        _positive_int_setting("LATE_INTERACTION_REMOTE_MAX_CANDIDATES", 500),
        _positive_int_setting("LATE_INTERACTION_MAX_RERANK_CHUNKS", 500),
    ]
    return min(limits)


def late_interaction_search_candidate_budget() -> int:
    """Bound the wider `/search` pool independently from pipeline scoring."""
    return min(
        _positive_int_setting("LATE_INTERACTION_SEARCH_CANDIDATE_LIMIT", 50),
        late_interaction_candidate_budget(),
    )


def late_interaction_canary_selected(repository_id: int, query: str) -> bool:
    if not bool(getattr(settings, "LATE_INTERACTION_RERANK_ENABLED", False)):
        return False
    percent = max(
        0.0,
        min(100.0, float(getattr(settings, "LATE_INTERACTION_CANARY_PERCENT", 0.0))),
    )
    sample_key = f"{repository_id}\0{query}".encode("utf-8", errors="replace")
    sample = int.from_bytes(hashlib.sha256(sample_key).digest()[:8], "big") / float(2**64)
    return sample < percent / 100.0


def late_interaction_requested(repository_id: int | None, query: str) -> bool:
    """Whether this request needs the otherwise-avoided wide candidate work."""
    if repository_id is None or not bool(getattr(settings, "LATE_INTERACTION_ENABLED", False)):
        return False
    return bool(getattr(settings, "LATE_INTERACTION_SHADOW_ENABLED", False)) or (
        late_interaction_canary_selected(repository_id, query)
    )


def infer_query_language(query: str) -> str:
    if re.search(r"[А-Яа-яЁё]", query):
        return "ru"
    if re.search(r"[A-Za-z]", query):
        return "en"
    return "unknown"


@dataclass
class LateInteractionApplicationResult:
    status: str = "disabled"
    baseline_top_k: list[str] = field(default_factory=list)
    counterfactual_top_k: list[str] = field(default_factory=list)
    effective_top_k: list[str] = field(default_factory=list)
    shadow: bool = False
    rerank_enabled: bool = False
    canary_percent: float = 0.0
    canary_selected: bool = False
    applied: bool = False
    coverage: float = 0.0
    latency_ms: float = 0.0
    model_revision: str = ""
    index_revision: str = ""
    reason: str = ""
    candidate_count: int = 0
    counterfactual_built: bool = False
    execution_mode: LateInteractionMode = "default"

    def to_debug(self) -> dict[str, Any]:
        # Preserve the pre-existing disabled debug contract exactly.
        debug: dict[str, Any] = {
            "status": self.status,
            "shadow": self.shadow,
            "rerank_enabled": self.rerank_enabled,
            "canary_percent": self.canary_percent,
            "applied": self.applied,
        }
        if self.status != "disabled":
            debug.update(
                {
                    "execution_mode": self.execution_mode,
                    "canary_selected": self.canary_selected,
                    "candidate_count": self.candidate_count,
                    "coverage": round(self.coverage, 4),
                    "latency_ms": round(self.latency_ms, 2),
                    "model_revision": self.model_revision,
                    "index_revision": self.index_revision,
                    "reason": self.reason,
                    "baseline_top_k": self.baseline_top_k,
                    "counterfactual_top_k": self.counterfactual_top_k,
                }
            )
        return debug


async def _record_shadow_fail_open(
    *,
    repository_id: int,
    query: str,
    request_id: str | None,
    query_class: str,
    baseline_top_k: list[str],
    counterfactual_top_k: list[str],
    status: str,
    coverage: float,
    latency_ms: float,
    model_revision: str,
    index_revision: str,
    reason: str,
    metadata: dict[str, Any],
) -> None:
    if not bool(
        getattr(settings, "LATE_INTERACTION_SHADOW_PERSIST_ENABLED", True)
    ):
        return
    try:
        from brain.late_interaction.shadow import (
            ShadowEvent,
            ShadowRankedItem,
            hash_query,
            make_shadow_idempotency_key,
            record_shadow_event,
        )

        normalized_status: Literal["scored", "skipped", "error"] = (
            "scored"
            if status in {"scored", "ok", "success"}
            and bool(metadata.get("counterfactual_built", False))
            else "skipped"
            if status in {"disabled", "skipped"}
            else "error"
        )

        def _ranked(paths: list[str]) -> list[ShadowRankedItem]:
            return [
                ShadowRankedItem(
                    item_id=item_id,
                    path=item_id.rsplit("#chunk:", 1)[0],
                    rank=rank,
                )
                for rank, item_id in enumerate(paths[:50], start=1)
            ]

        event = ShadowEvent(
            repo_id=repository_id,
            query_hash=hash_query(repository_id, query),
            idempotency_key=make_shadow_idempotency_key(
                repository_id,
                request_id or uuid.uuid4().hex,
            ),
            language=infer_query_language(query),
            query_class=query_class,
            baseline_final_top_k=_ranked(baseline_top_k),
            counterfactual_final_top_k=_ranked(counterfactual_top_k),
            score_metrics={
                "candidate_count": float(metadata.get("candidate_count", 0)),
                "canary_selected": float(bool(metadata.get("canary_selected", False))),
                "active_applied": float(bool(metadata.get("active_applied", False))),
            },
            status=normalized_status,
            coverage=coverage,
            timing_ms={"rerank": max(0.0, latency_ms)},
            model_revision=(
                model_revision
                or str(
                    getattr(
                        settings,
                        "LATE_INTERACTION_REMOTE_MODEL_REVISION",
                        settings.LATE_INTERACTION_MODEL_REVISION,
                    )
                )
            ),
            index_revision=(
                index_revision
                or str(
                    getattr(
                        settings,
                        "LATE_INTERACTION_REMOTE_EXPECTED_INDEX_REVISION",
                        "",
                    )
                    or "unknown"
                )
            ),
            skip_reason=None if normalized_status == "scored" else (reason or status),
        )
        await record_shadow_event(event)
    except Exception as exc:
        logger.warning(
            "Late-interaction shadow write failed open: {}",
            type(exc).__name__,
        )


async def apply_late_interaction(
    *,
    query: str,
    repository_id: int | None,
    candidates: Sequence[Any],
    baseline_top_k: Sequence[str],
    build_counterfactual: CounterfactualBuilder,
    query_class: str,
    index_revision: str = "",
    request_id: str | None = None,
    mode: LateInteractionMode = "default",
    client: Any | None = None,
    timeout_s: float | None = None,
) -> LateInteractionApplicationResult:
    """Score a bounded candidate set and choose shadow/baseline/active output.

    Imports of ``client`` and ``shadow`` stay lazy so all flags-off requests are
    byte-identical and do not require the optional GPU integration modules.
    ``request_id`` must be a real caller-owned request identity when supplied;
    callers without one leave it unset so separate requests cannot deduplicate
    accidentally.
    """
    baseline = list(baseline_top_k)
    deep = mode == "deep"
    shadow = bool(getattr(settings, "LATE_INTERACTION_SHADOW_ENABLED", False))
    rerank_enabled = deep or bool(
        getattr(settings, "LATE_INTERACTION_RERANK_ENABLED", False)
    )
    canary_percent = float(getattr(settings, "LATE_INTERACTION_CANARY_PERCENT", 0.0))
    result = LateInteractionApplicationResult(
        baseline_top_k=baseline,
        counterfactual_top_k=baseline,
        effective_top_k=baseline,
        shadow=shadow,
        rerank_enabled=rerank_enabled,
        canary_percent=canary_percent,
        execution_mode=mode,
    )
    if repository_id is None or (
        not deep and not late_interaction_requested(repository_id, query)
    ):
        return result

    result.canary_selected = deep or late_interaction_canary_selected(
        repository_id, query
    )
    bounded_candidates = list(candidates)[:late_interaction_candidate_budget()]
    result.candidate_count = len(bounded_candidates)
    if not bounded_candidates:
        result.status = "skipped"
        result.reason = "no_candidates"
        if shadow or deep:
            await _record_shadow_fail_open(
                repository_id=repository_id,
                query=query,
                request_id=request_id,
                query_class=query_class,
                baseline_top_k=baseline,
                counterfactual_top_k=baseline,
                status=result.status,
                coverage=0.0,
                latency_ms=0.0,
                model_revision="",
                index_revision=index_revision,
                reason=result.reason,
                metadata={"candidate_count": 0, "canary_selected": result.canary_selected},
            )
        return result

    from brain.late_interaction.metrics import increment, observe

    started = time.perf_counter()
    increment("rerank_requests")
    remote_result: Any = None
    try:
        from brain.late_interaction.client import get_late_interaction_client

        effective_timeout_s = (
            float(timeout_s)
            if timeout_s is not None
            else float(
                getattr(
                    settings,
                    "LATE_INTERACTION_DEEP_TIMEOUT_S"
                    if deep
                    else "LATE_INTERACTION_RERANK_TIMEOUT_S",
                    120.0 if deep else 2.0,
                )
            )
        )
        active_client = client or get_late_interaction_client()
        remote_result = await asyncio.wait_for(
            active_client.rerank(
                query=query,
                repository_id=repository_id,
                candidates=bounded_candidates,
            ),
            timeout=effective_timeout_s,
        )
        result.status = str(getattr(remote_result, "status", "failed_open"))
        result.coverage = float(getattr(remote_result, "coverage", 0.0) or 0.0)
        result.model_revision = str(getattr(remote_result, "model_revision", "") or "")
        result.index_revision = str(getattr(remote_result, "index_revision", "") or "")
        result.reason = str(getattr(remote_result, "reason", "") or "")
        remote_latency = float(getattr(remote_result, "latency_ms", 0.0) or 0.0)
        result.latency_ms = remote_latency or ((time.perf_counter() - started) * 1000)

        if result.status in {"scored", "ok", "success"} and getattr(remote_result, "scores", None):
            built = build_counterfactual(remote_result)
            if inspect.isawaitable(built):
                built = await built
            counterfactual = list(built)
            if counterfactual:
                result.counterfactual_built = True
                result.counterfactual_top_k = counterfactual
                minimum_coverage = float(
                    getattr(
                        settings,
                        "LATE_INTERACTION_MIN_CANDIDATE_COVERAGE",
                        1.0,
                    )
                )
                if result.coverage >= minimum_coverage:
                    result.effective_top_k = (
                        counterfactual if result.canary_selected else baseline
                    )
                    result.applied = result.canary_selected
                else:
                    result.status = "partial"
                    result.reason = "insufficient_candidate_coverage"
        else:
            result.counterfactual_top_k = baseline
    except asyncio.TimeoutError:
        result.status = "timed_out"
        result.reason = "late-interaction request exceeded surface timeout"
        result.latency_ms = (time.perf_counter() - started) * 1000
    except Exception as exc:
        result.status = "failed_open"
        result.reason = type(exc).__name__
        result.latency_ms = (time.perf_counter() - started) * 1000

    observe(
        "rerank_latency_ms_total",
        (time.perf_counter() - started) * 1000,
    )
    if result.status == "timed_out" or result.reason == "timeout":
        increment("rerank_timeouts")
    if result.status in {
        "failed_open",
        "timed_out",
        "revision_mismatch",
        "inventory_mismatch",
    }:
        increment("rerank_fail_open")
    if result.applied:
        increment("rerank_applied")

    if shadow or deep:
        await _record_shadow_fail_open(
            repository_id=repository_id,
            query=query,
            request_id=request_id,
            query_class=query_class,
            baseline_top_k=baseline,
            counterfactual_top_k=result.counterfactual_top_k,
            status=result.status,
            coverage=result.coverage,
            latency_ms=result.latency_ms,
            model_revision=result.model_revision,
            index_revision=result.index_revision or index_revision,
            reason=result.reason,
            metadata={
                "candidate_count": result.candidate_count,
                "canary_selected": result.canary_selected,
                "active_applied": result.applied,
                "counterfactual_built": result.counterfactual_built,
            },
        )
    return result
