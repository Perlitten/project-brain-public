"""Durable, bounded telemetry for late-interaction shadow ranking.

The surface adapters construct :class:`ShadowEvent` only after both baseline
and counterfactual final rankings are known.  The public contract intentionally
has no raw-query field: callers provide a SHA-256 digest and the recorder stores
only that digest.  Persistence is fail-open so telemetry can never make search
unavailable.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Literal, cast

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import CursorResult

from brain.database.models import LateInteractionShadowEvent
from brain.database.session import async_session_factory


MAX_SHADOW_TOP_K = 50
MAX_SHADOW_METRICS = 32
MAX_SHADOW_TIMINGS = 16
MAX_SHADOW_JSON_BYTES = 64 * 1024
DEFAULT_SHADOW_RETENTION_DAYS = 30
ShadowStatus = Literal["scored", "skipped", "error"]


def hash_query(repo_id: int, query: str) -> str:
    """Return a repository-scoped query identity for durable telemetry.

    Scoping prevents the same query text from being correlated across
    repositories.  The raw query is not returned or stored.
    """
    if repo_id < 1:
        raise ValueError("repo_id must be positive")
    return hashlib.sha256(f"{repo_id}\x00{query}".encode("utf-8")).hexdigest()


def make_shadow_idempotency_key(repo_id: int, request_id: str) -> str:
    """Hash a request identity so retries are durable but request IDs are not."""
    if repo_id < 1:
        raise ValueError("repo_id must be positive")
    if not request_id:
        raise ValueError("request_id is required")
    return hashlib.sha256(
        f"late-shadow\x00{repo_id}\x00{request_id}".encode("utf-8")
    ).hexdigest()


class ShadowRankedItem(BaseModel):
    """One bounded item from the final baseline/counterfactual ranking."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    item_id: str = Field(min_length=1, max_length=255)
    path: str = Field(min_length=1, max_length=1024)
    rank: int = Field(ge=1, le=MAX_SHADOW_TOP_K)
    score: float | None = None

    @field_validator("score")
    @classmethod
    def _finite_score(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("score must be finite")
        return value


class ShadowEvent(BaseModel):
    """Public surface-to-shadow-store contract.

    Ranking arrays must be ordered and densely ranked.  Numeric maps are kept
    deliberately small and accept only finite numbers so JSON storage remains
    portable and bounded.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    repo_id: int = Field(ge=1)
    query_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    language: str = Field(min_length=2, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")
    query_class: str = Field(min_length=2, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    baseline_final_top_k: list[ShadowRankedItem] = Field(max_length=MAX_SHADOW_TOP_K)
    counterfactual_final_top_k: list[ShadowRankedItem] = Field(max_length=MAX_SHADOW_TOP_K)
    score_metrics: dict[str, float] = Field(default_factory=dict)
    status: ShadowStatus
    coverage: float = Field(ge=0.0, le=1.0)
    timing_ms: dict[str, float] = Field(default_factory=dict)
    model_revision: str = Field(min_length=1, max_length=255)
    index_revision: str = Field(min_length=1, max_length=255)
    skip_reason: str | None = Field(default=None, max_length=512)

    @field_validator("baseline_final_top_k", "counterfactual_final_top_k")
    @classmethod
    def _dense_unique_ranking(
        cls,
        value: list[ShadowRankedItem],
    ) -> list[ShadowRankedItem]:
        ranks = [item.rank for item in value]
        if ranks != list(range(1, len(value) + 1)):
            raise ValueError("ranking must be ordered and densely ranked from 1")
        item_ids = [item.item_id for item in value]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("ranking item_id values must be unique")
        return value

    @field_validator("score_metrics")
    @classmethod
    def _bounded_metrics(cls, value: dict[str, float]) -> dict[str, float]:
        return _validate_numeric_map(value, limit=MAX_SHADOW_METRICS, non_negative=False)

    @field_validator("timing_ms")
    @classmethod
    def _bounded_timings(cls, value: dict[str, float]) -> dict[str, float]:
        return _validate_numeric_map(value, limit=MAX_SHADOW_TIMINGS, non_negative=True)

    @model_validator(mode="after")
    def _status_contract(self) -> "ShadowEvent":
        if self.status == "scored" and self.skip_reason is not None:
            raise ValueError("scored events cannot have skip_reason")
        if self.status == "skipped" and not self.skip_reason:
            raise ValueError("skipped events require skip_reason")
        payload_size = len(self.model_dump_json().encode("utf-8"))
        if payload_size > MAX_SHADOW_JSON_BYTES:
            raise ValueError(
                f"shadow event JSON exceeds {MAX_SHADOW_JSON_BYTES} bytes"
            )
        return self


def _validate_numeric_map(
    value: dict[str, float],
    *,
    limit: int,
    non_negative: bool,
) -> dict[str, float]:
    if len(value) > limit:
        raise ValueError(f"numeric map exceeds {limit} entries")
    bounded: dict[str, float] = {}
    for key, raw in value.items():
        if not isinstance(key, str) or not key or len(key) > 64:
            raise ValueError("numeric map keys must be 1..64 character strings")
        number = float(raw)
        if not math.isfinite(number):
            raise ValueError(f"{key} must be finite")
        if non_negative and number < 0:
            raise ValueError(f"{key} cannot be negative")
        bounded[key] = number
    return bounded


async def record_shadow_event(event: ShadowEvent | Mapping[str, object]) -> bool:
    """Persist one shadow event, returning ``False`` on any failure.

    Both validation and database errors are swallowed after a bounded warning.
    This function is safe to await from a search surface: the authoritative
    baseline result is returned regardless of telemetry availability.
    """
    try:
        validated = (
            event
            if isinstance(event, ShadowEvent)
            else ShadowEvent.model_validate(event)
        )
        payload = validated.model_dump(mode="json")
        # Defence in depth: never pass an accidentally unbounded payload to the
        # ORM even if the model contract changes later.
        if len(json.dumps(payload, separators=(",", ":")).encode("utf-8")) > MAX_SHADOW_JSON_BYTES:
            raise ValueError("shadow event JSON exceeds durable storage bound")
        statement = insert(LateInteractionShadowEvent).values(
            repository_id=validated.repo_id,
            query_hash=validated.query_hash,
            idempotency_key=validated.idempotency_key,
            language=validated.language,
            query_class=validated.query_class,
            baseline_final_top_k=payload["baseline_final_top_k"],
            counterfactual_final_top_k=payload["counterfactual_final_top_k"],
            score_metrics=payload["score_metrics"],
            status=validated.status,
            coverage=validated.coverage,
            timing_ms=payload["timing_ms"],
            model_revision=validated.model_revision,
            index_revision=validated.index_revision,
            skip_reason=validated.skip_reason,
        ).on_conflict_do_nothing(
            index_elements=["idempotency_key"],
        )
        async with async_session_factory() as session:
            await session.execute(statement)
            await session.commit()
        return True
    except Exception as exc:
        logger.warning(
            "Late-interaction shadow event was not persisted (fail-open): "
            f"{type(exc).__name__}: {exc}"
        )
        return False


async def cleanup_shadow_events(
    *,
    retention_days: int = DEFAULT_SHADOW_RETENTION_DAYS,
    repo_id: int | None = None,
    batch_limit: int = 10_000,
) -> int:
    """Delete one bounded batch older than the retention window.

    Cleanup is also fail-open: an ops sweep reports zero removals if telemetry
    storage is unavailable and never affects retrieval data.
    """
    if retention_days < 1:
        raise ValueError("retention_days must be positive")
    if repo_id is not None and repo_id < 1:
        raise ValueError("repo_id must be positive")
    if not 1 <= batch_limit <= 100_000:
        raise ValueError("batch_limit must be between 1 and 100000")

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    try:
        id_query = (
            select(LateInteractionShadowEvent.id)
            .where(LateInteractionShadowEvent.created_at < cutoff)
            .order_by(LateInteractionShadowEvent.created_at)
            .limit(batch_limit)
        )
        if repo_id is not None:
            id_query = id_query.where(
                LateInteractionShadowEvent.repository_id == repo_id
            )
        async with async_session_factory() as session:
            ids = list((await session.scalars(id_query)).all())
            if not ids:
                return 0
            result = cast(
                CursorResult,
                await session.execute(
                    delete(LateInteractionShadowEvent).where(
                        LateInteractionShadowEvent.id.in_(ids)
                    )
                ),
            )
            await session.commit()
            return int(result.rowcount or 0)
    except Exception as exc:
        logger.warning(
            "Late-interaction shadow retention cleanup failed open: "
            f"{type(exc).__name__}: {exc}"
        )
        return 0


__all__ = [
    "DEFAULT_SHADOW_RETENTION_DAYS",
    "MAX_SHADOW_JSON_BYTES",
    "MAX_SHADOW_TOP_K",
    "ShadowEvent",
    "ShadowRankedItem",
    "cleanup_shadow_events",
    "hash_query",
    "make_shadow_idempotency_key",
    "record_shadow_event",
]
