"""One ``freshness`` block for every agent-facing answer.

Agents read the same envelope from every MCP tool and REST endpoint that
serves indexed knowledge, and decide from ``state`` alone whether to trust the
answer, re-check the source, or wait for the re-index named by
``reindex_job_id``. The envelope is transport-independent and only *reads*
what :mod:`brain.memory.repo_freshness` already computes; anything that module
adds (origin tracking, for instance) is passed through untouched.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar, Literal, Optional

from pydantic import BaseModel, ConfigDict

EnvelopeState = Literal["fresh", "stale_served", "stale_blocked", "unknown"]

#: Keys of the freshness module's dict that the envelope renames or folds.
_CONSUMED_KEYS = frozenset({"status", "indexed_commit", "last_indexed_at", "age_seconds"})

#: Repository freshness status -> (envelope state, short reason code).
_STATUS_TO_STATE: dict[str, tuple[EnvelopeState, str]] = {
    "current": ("fresh", "index_matches_source"),
    "behind": ("stale_served", "source_ahead_of_index"),
    "stale": ("stale_served", "index_age_exceeded"),
    "unindexed": ("unknown", "unindexed"),
    "unverifiable": ("unknown", "source_unverifiable"),
    "source_missing": ("unknown", "source_missing"),
}

#: The freshness module reports ``stale`` for two different situations; the
#: note is the only place it says which.
_UNVERIFIED_NOTE_PREFIX = "Unable to validate freshness"


class FreshnessEnvelope(BaseModel):
    """What an agent needs to decide whether to trust an answer.

    The first six fields are always present (``null`` when unknown). The rest
    are the freshness module's own observations, kept only when they carry a
    value, plus any key that module adds later (``extra="allow"``).
    """

    model_config = ConfigDict(extra="allow")

    state: EnvelopeState = "unknown"
    reason: str = "freshness_unavailable"
    indexed_commit: Optional[str] = None
    indexed_at: Optional[str] = None
    age_s: Optional[int] = None
    reindex_job_id: Optional[str] = None
    # Pass-through from the freshness module.
    index_status: Optional[str] = None
    source_head_commit: Optional[str] = None
    branch: Optional[str] = None
    commits_behind: Optional[int] = None
    source_revision_kind: Optional[str] = None
    source_present: Optional[bool] = None
    note: Optional[str] = None

    CORE_FIELDS: ClassVar[tuple[str, ...]] = (
        "state", "reason", "indexed_commit", "indexed_at", "age_s", "reindex_job_id",
    )

    def to_payload(self) -> dict[str, Any]:
        """The wire form: core fields always, everything else only when set."""
        dumped = self.model_dump()
        return {
            key: value
            for key, value in dumped.items()
            if key in self.CORE_FIELDS or value is not None
        }


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _int_or_none(value: Any) -> Optional[int]:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _same_revision(left: str, right: str) -> bool:
    return bool(left and right) and (left.startswith(right) or right.startswith(left))


def _origin_ahead(freshness: dict[str, Any]) -> bool:
    behind = freshness.get("commits_behind_origin")
    if isinstance(behind, int) and not isinstance(behind, bool):
        return behind > 0
    origin_head = str(freshness.get("origin_head") or "").strip().lower()
    indexed = str(freshness.get("indexed_commit") or "").strip().lower()
    return bool(origin_head and indexed) and not _same_revision(origin_head, indexed)


def derive_state(freshness: dict[str, Any] | None) -> tuple[EnvelopeState, str]:
    """Map the freshness module's ``status`` (and note) to an envelope state."""
    if not freshness:
        return "unknown", "freshness_unavailable"
    status = str(freshness.get("status") or "unknown")
    if status == "stale" and str(freshness.get("note") or "").startswith(_UNVERIFIED_NOTE_PREFIX):
        return "stale_served", "source_head_unverified"
    if status == "current" and _origin_ahead(freshness):
        # The index matches the source it read, but origin has moved on: the
        # answer is served while the re-index catches up.
        return "stale_served", "origin_ahead_of_index"
    return _STATUS_TO_STATE.get(status, ("unknown", "freshness_unavailable"))


def freshness_envelope(
    freshness: dict[str, Any] | None,
    *,
    state: EnvelopeState | None = None,
    reason: str | None = None,
    reindex_job_id: str | None = None,
) -> dict[str, Any]:
    """Build the envelope from a ``repo_freshness`` dict.

    ``state``/``reason`` override the mapping when the caller knows better
    (the runtime context builder, which also sees the working tree); either
    may be given alone.
    """
    raw = dict(freshness or {})
    derived_state, derived_reason = derive_state(raw)
    fields: dict[str, Any] = {
        "state": state or derived_state,
        "reason": reason or derived_reason,
        "indexed_commit": raw.get("indexed_commit") or None,
        "indexed_at": _iso(raw.get("last_indexed_at")),
        "age_s": _int_or_none(raw.get("age_seconds")),
        # The origin poll names the re-index it requested for a newer origin head.
        "reindex_job_id": reindex_job_id or raw.get("reindex_job_id") or None,
        "index_status": raw.get("status") or None,
    }
    for key, value in raw.items():
        if key in _CONSUMED_KEYS or key in fields or value is None:
            continue
        fields[key] = value
    return FreshnessEnvelope.model_validate(fields).to_payload()


def unknown_envelope(reason: str = "freshness_unavailable") -> dict[str, Any]:
    return freshness_envelope(None, state="unknown", reason=reason)


async def envelope_for_repository(repo: Any, *, reindex_job_id: str | None = None) -> dict[str, Any]:
    """Assess ``repo`` (an ORM row) and wrap the result; never raises."""
    from brain.memory.repo_freshness import assess_repository_freshness

    try:
        return freshness_envelope(await assess_repository_freshness(repo), reindex_job_id=reindex_job_id)
    except Exception:  # noqa: BLE001 - freshness must never break the answer it describes
        return unknown_envelope()


#: A tool answer must not wait on the freshness lookup for long: past this the
#: envelope says ``unknown`` and the answer goes out.
LOOKUP_TIMEOUT_S = 3.0


async def envelope_for_path(repo_path: str | Path | None) -> dict[str, Any]:
    """The envelope for an indexed repository at ``repo_path``; never raises."""
    if not repo_path:
        return unknown_envelope("repository_not_specified")
    try:
        from brain.database.repository_utils import get_repository_by_path

        repo = await asyncio.wait_for(get_repository_by_path(str(repo_path)), timeout=LOOKUP_TIMEOUT_S)
    except Exception:  # noqa: BLE001
        return unknown_envelope()
    if repo is None:
        return unknown_envelope("repository_not_indexed")
    return await envelope_for_repository(repo)


def envelope_for_search_scope(code_results: Any) -> dict[str, Any]:
    """The envelope from a hybrid search result's ``repository_scope``."""
    scope = code_results.get("repository_scope") if isinstance(code_results, dict) else None
    scope = scope if isinstance(scope, dict) else {}
    if scope.get("found") is False:
        return unknown_envelope("repository_not_indexed")
    raw = dict(scope.get("freshness") or {})
    raw.setdefault("indexed_commit", scope.get("last_indexed_commit"))
    return freshness_envelope(raw)


def envelope_for_retrieval(repository: Any) -> dict[str, Any]:
    """The envelope from a :class:`RetrievalResult` ``repository`` projection."""
    repository = repository if isinstance(repository, dict) else {}
    raw = dict(repository.get("freshness") or {})
    raw.setdefault("indexed_commit", repository.get("indexed_revision") or repository.get("last_indexed_commit"))
    return freshness_envelope(raw)


def repository_age_report(rows: Any, max_age_s: int, now: datetime | None = None) -> dict[str, Any]:
    """Per-repository index age against the ``max_age_s`` SLO (``<= 0`` disables it).

    ``rows`` are ``Repository`` records; the age is measured from the same
    column the freshness module falls back to, so ``/health`` and the tool
    envelopes agree on how old an index is.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    limit = _int_or_none(max_age_s) or 0
    repositories: list[dict[str, Any]] = []
    exceeded: list[str] = []
    for row in rows or ():
        indexed_at = getattr(row, "last_indexed_at", None) or getattr(row, "updated_at", None)
        age_s: Optional[int] = None
        if isinstance(indexed_at, datetime):
            stamp = indexed_at if indexed_at.tzinfo else indexed_at.replace(tzinfo=timezone.utc)
            age_s = max(0, int((now - stamp).total_seconds()))
        within = None if limit <= 0 or age_s is None else age_s <= limit
        path = str(getattr(row, "path", "") or "")
        if within is False:
            exceeded.append(path)
        repositories.append({
            "path": path,
            "indexed_commit": getattr(row, "last_indexed_commit", None) or None,
            "indexed_at": _iso(indexed_at),
            "age_s": age_s,
            "within_slo": within,
        })
    return {"max_age_s": limit, "exceeded": exceeded, "repositories": repositories}


def attach_envelope(payload: Any, envelope: dict[str, Any]) -> Any:
    """Add ``freshness`` to a dict result in place; leave anything else alone."""
    if isinstance(payload, dict):
        payload["freshness"] = envelope
    return payload


__all__ = [
    "EnvelopeState",
    "FreshnessEnvelope",
    "attach_envelope",
    "derive_state",
    "envelope_for_path",
    "envelope_for_repository",
    "envelope_for_retrieval",
    "envelope_for_search_scope",
    "freshness_envelope",
    "repository_age_report",
    "unknown_envelope",
]
