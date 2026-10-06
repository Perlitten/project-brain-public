"""Freshness Tracker, Invalidation Bus, and Cache — Phases C3–C5."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from brain.freshness.models import (
    ArtifactType,
    FreshnessRecord,
    FreshnessState,
    InvalidationEvent,
    _utc_now,
)


class FreshnessTracker:
    """Phase C3 — tracks evidence-based freshness state for artifacts.

    ``current`` is only recorded when the caller supplies an ``observed_revision``
    that matches the artifact's ``source_revision``. Without verifiable evidence
    the state is downgraded to ``unknown`` — never optimistically ``current``.
    """

    def __init__(self, storage_dir: Path):
        self._dir = storage_dir.resolve()
        self._dir.mkdir(parents=True, exist_ok=True)
        self._file = self._dir / "freshness.json"
        self._lock = threading.Lock()

    # ── recording ──

    def record(
        self,
        artifact_id: str,
        artifact_type: str,
        repository_id: str,
        state: FreshnessState,
        source_revision: str,
        evidence: str,
        reason: str = "",
        observed_revision: str = "",
    ) -> FreshnessRecord:
        state, reason = self._verify_state(state, source_revision, observed_revision, reason)
        with self._lock:
            data = self._load()
            previous = data.get(artifact_id)
            rec = FreshnessRecord(
                artifact_id=artifact_id,
                artifact_type=artifact_type,
                repository_id=repository_id,
                state=state.value,
                source_revision=source_revision,
                observed_revision=observed_revision,
                evidence=evidence,
                last_checked_utc=_utc_now(),
                reason=reason,
                version=(previous.get("version", 0) + 1) if previous else 1,
            )
            data[artifact_id] = rec.to_dict()
            self._save(data)
        return rec

    def record_from_repository(
        self,
        artifact_id: str,
        artifact_type: ArtifactType,
        repository_id: str,
        repo_path: Path,
        source_revision: str,
        evidence: str,
    ) -> FreshnessRecord:
        """Record freshness by resolving the repository's real HEAD as evidence."""
        observed = self.resolve_revision(repo_path)
        state = FreshnessState.CURRENT if observed else FreshnessState.UNKNOWN
        return self.record(
            artifact_id=artifact_id,
            artifact_type=artifact_type.value,
            repository_id=repository_id,
            state=state,
            source_revision=source_revision,
            evidence=evidence,
            observed_revision=observed,
            reason="" if observed else "Repository revision could not be resolved",
        )

    # ── reading ──

    def get(self, artifact_id: str) -> Optional[FreshnessRecord]:
        raw = self._load().get(artifact_id)
        return FreshnessRecord.from_dict(raw) if raw else None

    def list_by_repository(self, repository_id: str) -> List[FreshnessRecord]:
        return [
            FreshnessRecord.from_dict(v)
            for v in self._load().values()
            if v.get("repository_id") == repository_id
        ]

    def list_all(self) -> List[FreshnessRecord]:
        return [FreshnessRecord.from_dict(v) for v in self._load().values()]

    def explain(self, artifact_id: str) -> Dict[str, Any]:
        """Human-readable freshness explanation — backs ``freshness explain``."""
        rec = self.get(artifact_id)
        if not rec:
            return {"artifact_id": artifact_id, "found": False}
        return {
            "artifact_id": artifact_id,
            "found": True,
            "state": rec.state,
            "artifact_type": rec.artifact_type,
            "repository_id": rec.repository_id,
            "source_revision": rec.source_revision,
            "observed_revision": rec.observed_revision,
            "revision_match": bool(rec.observed_revision)
            and rec.observed_revision == rec.source_revision,
            "evidence": rec.evidence,
            "reason": rec.reason,
            "superseded_by": rec.superseded_by,
            "last_checked_utc": rec.last_checked_utc,
            "version": rec.version,
        }

    # ── transitions ──

    def invalidate(
        self, artifact_id: str, caused_by: str, details: str = ""
    ) -> Optional[FreshnessRecord]:
        return self._transition(
            artifact_id,
            FreshnessState.STALE,
            reason=f"Invalidated by {caused_by}: {details}".strip(": "),
        )

    def mark_building(self, artifact_id: str, caused_by: str = "") -> Optional[FreshnessRecord]:
        return self._transition(
            artifact_id, FreshnessState.BUILDING, reason=f"Rebuild started by {caused_by}".strip()
        )

    def mark_failed(self, artifact_id: str, reason: str) -> Optional[FreshnessRecord]:
        return self._transition(artifact_id, FreshnessState.FAILED, reason=reason)

    def supersede(self, artifact_id: str, superseded_by: str) -> Optional[FreshnessRecord]:
        return self._transition(
            artifact_id,
            FreshnessState.SUPERSEDED,
            reason=f"Superseded by {superseded_by}",
            superseded_by=superseded_by,
        )

    def invalidate_repository(self, repository_id: str, caused_by: str, details: str = "") -> int:
        """Invalidate every artifact belonging to a repository. Returns the count."""
        count = 0
        for rec in self.list_by_repository(repository_id):
            if rec.state in {FreshnessState.STALE.value, FreshnessState.SUPERSEDED.value}:
                continue
            if self.invalidate(rec.artifact_id, caused_by, details):
                count += 1
        return count

    # ── helpers ──

    @staticmethod
    def resolve_revision(repo_path: Path) -> str:
        """Resolve a repository HEAD, or '' when it cannot be verified."""
        try:
            proc = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return ""
        return proc.stdout.strip() if proc.returncode == 0 else ""

    @staticmethod
    def _verify_state(
        state: FreshnessState, source_revision: str, observed_revision: str, reason: str
    ) -> tuple[FreshnessState, str]:
        if state is not FreshnessState.CURRENT:
            return state, reason
        if not observed_revision or not source_revision:
            return FreshnessState.UNKNOWN, reason or "Source revision could not be verified"
        if observed_revision != source_revision:
            return (
                FreshnessState.STALE,
                reason
                or f"Observed revision {observed_revision[:12]} != recorded {source_revision[:12]}",
            )
        return state, reason

    def _transition(
        self,
        artifact_id: str,
        state: FreshnessState,
        reason: str,
        superseded_by: str = "",
    ) -> Optional[FreshnessRecord]:
        with self._lock:
            data = self._load()
            if artifact_id not in data:
                return None
            entry = data[artifact_id]
            entry["state"] = state.value
            entry["reason"] = reason
            entry["last_checked_utc"] = _utc_now()
            entry["version"] = entry.get("version", 1) + 1
            if superseded_by:
                entry["superseded_by"] = superseded_by
            self._save(data)
            return FreshnessRecord.from_dict(entry)

    def _load(self) -> Dict[str, Any]:
        if not self._file.exists():
            return {}
        try:
            return json.loads(self._file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _save(self, data: Dict[str, Any]) -> None:
        tmp = self._file.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, sort_keys=True, indent=2))
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(self._file)


class InvalidationBus:
    """Phase C4 — internal event-driven invalidation bus.

    Deliberately in-process and dependency-free: Git hooks, CI, or the worker scheduler can call
    :meth:`emit` later without this module knowing about them.
    """

    def __init__(self) -> None:
        self._handlers: Dict[str, List[Callable]] = {}
        self._log: List[InvalidationEvent] = []
        self._lock = threading.Lock()

    def register_handler(self, event_type: str, callback: Callable) -> None:
        with self._lock:
            self._handlers.setdefault(event_type, []).append(callback)

    def emit(self, event: InvalidationEvent) -> List[Any]:
        with self._lock:
            handlers = list(self._handlers.get(event.event_type, []))
            self._log.append(event)
        results = []
        for handler in handlers:
            try:
                results.append(handler(event))
            except Exception as exc:  # a bad subscriber must not break invalidation
                results.append({"error": str(exc), "handler": getattr(handler, "__name__", "?")})
        return results

    def history(self, limit: int = 100) -> List[InvalidationEvent]:
        with self._lock:
            return list(self._log[-limit:])


class IncrementalCache:
    """Phase C5 — bounded cache for expensive deterministic results.

    Keys are built from *all* semantic inputs via :meth:`build_key`; a bare path
    is never a valid key because the same path yields different results at
    different revisions.
    """

    def __init__(
        self,
        cache_dir: Path,
        max_entries: int = 10000,
        max_bytes: int = 100_000_000,
        namespace: str = "default",
    ):
        self._dir = cache_dir.resolve()
        self._dir.mkdir(parents=True, exist_ok=True)
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._namespace = namespace
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._corruptions = 0
        self._lock = threading.Lock()

    # ── key construction ──

    @staticmethod
    def build_key(kind: str, **semantic_inputs: Any) -> str:
        """Build a cache key from every semantic input.

        Raises when no input beyond a path is supplied, so cache-by-path — which
        silently returns stale results across revisions — cannot happen.
        """
        if not semantic_inputs:
            raise ValueError("Cache key requires semantic inputs, not just a kind")
        non_path = {k: v for k, v in semantic_inputs.items() if k not in {"path", "file_path"}}
        if not non_path:
            raise ValueError(
                "Cache key requires at least one non-path semantic input "
                "(e.g. content_hash, revision, policy_version)"
            )
        payload = json.dumps({"kind": kind, **semantic_inputs}, sort_keys=True, default=str)
        return f"{kind}:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"

    # ── operations ──

    def get(self, key: str) -> Optional[Any]:
        f = self._path_for(key)
        if not f.exists():
            self._misses += 1
            return None
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            # Corruption is recoverable: drop the entry and report a miss.
            self._corruptions += 1
            self._misses += 1
            try:
                f.unlink()
            except OSError:
                pass
            return None
        expires_at = data.get("expires_at")
        if expires_at and time.time() > expires_at:
            try:
                f.unlink()
            except OSError:
                pass
            self._misses += 1
            return None
        self._hits += 1
        return data.get("value")

    def put(self, key: str, value: Any, ttl_seconds: Optional[int] = None) -> None:
        with self._lock:
            entry: Dict[str, Any] = {"key": key, "value": value, "created_at": time.time()}
            if ttl_seconds:
                entry["expires_at"] = time.time() + ttl_seconds
            f = self._path_for(key)
            tmp = f.with_suffix(".tmp")
            tmp.write_text(json.dumps(entry, sort_keys=True, default=str), encoding="utf-8")
            tmp.replace(f)
            self._evict_if_needed()

    def invalidate(self, key: str) -> bool:
        f = self._path_for(key)
        if f.exists():
            f.unlink()
            return True
        return False

    def invalidate_kind(self, kind: str) -> int:
        """Explicit bulk invalidation for one cached computation kind."""
        count = 0
        for f in self._entries():
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                continue
            if str(data.get("key", "")).startswith(f"{kind}:"):
                f.unlink()
                count += 1
        return count

    def clear(self) -> int:
        count = 0
        for f in self._entries():
            f.unlink()
            count += 1
        return count

    def stats(self) -> Dict[str, Any]:
        entries = self._entries()
        total = self._hits + self._misses
        return {
            "namespace": self._namespace,
            "entries": len(entries),
            "total_bytes": sum(f.stat().st_size for f in entries),
            "max_entries": self._max_entries,
            "max_bytes": self._max_bytes,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / total, 4) if total else 0.0,
            "evictions": self._evictions,
            "corruptions": self._corruptions,
        }

    # ── internals ──

    def _path_for(self, key: str) -> Path:
        return self._dir / f"{hashlib.sha256(key.encode('utf-8')).hexdigest()[:32]}.cache.json"

    def _entries(self) -> List[Path]:
        return sorted(self._dir.glob("*.cache.json"), key=lambda f: f.stat().st_mtime)

    def _evict_if_needed(self) -> None:
        entries = self._entries()
        total_bytes = sum(f.stat().st_size for f in entries)
        while entries and (len(entries) > self._max_entries or total_bytes > self._max_bytes):
            victim = entries.pop(0)
            try:
                total_bytes -= victim.stat().st_size
                victim.unlink()
                self._evictions += 1
            except OSError:
                break
