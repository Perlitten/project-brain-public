"""Read bounded, retained LFM evaluation evidence for operator displays."""

from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any


MAX_EVIDENCE_BYTES = 4_000_000
EVIDENCE_STALE_AFTER_SECONDS = 24 * 60 * 60


def _absolute_without_resolving(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _safe_file(path: Path, root: Path) -> Path | None:
    """Resolve a regular file inside root and reject every symlink component."""

    try:
        absolute = _absolute_without_resolving(path)
        root_absolute = _absolute_without_resolving(root)
        relative = absolute.relative_to(root_absolute)
    except (OSError, ValueError):
        return None

    current = root_absolute
    try:
        if current.is_symlink():
            return None
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                return None
        resolved_root = root_absolute.resolve(strict=True)
        resolved = absolute.resolve(strict=True)
        resolved.relative_to(resolved_root)
        if not resolved.is_file():
            return None
        return resolved
    except (OSError, RuntimeError, ValueError):
        return None


def _read_json(path: Path, root: Path) -> tuple[dict[str, Any], dict[str, Any]] | None:
    safe = _safe_file(path, root)
    if safe is None:
        return None
    size = safe.stat().st_size
    if size <= 0 or size > MAX_EVIDENCE_BYTES:
        return None
    raw = safe.read_bytes()
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    metadata = {
        "name": safe.name,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": size,
        "modified_at": datetime.fromtimestamp(
            safe.stat().st_mtime,
            tz=timezone.utc,
        ).isoformat(),
        "modified_timestamp": safe.stat().st_mtime,
    }
    return payload, metadata


def _as_dict(value: Any) -> dict[Any, Any]:
    return value if isinstance(value, dict) else {}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
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


def _checks_failed(gate: Any) -> list[str]:
    if not isinstance(gate, dict):
        return []
    checks = gate.get("checks")
    if not isinstance(checks, dict):
        return []
    return sorted(str(name) for name, passed in checks.items() if passed is not True)


def _quality_summary(report: dict[str, Any]) -> dict[str, Any]:
    dataset = _as_dict(report.get("dataset"))
    evidence = _as_dict(report.get("evidence"))
    aggregate = _as_dict(report.get("aggregate"))
    baseline = _as_dict(aggregate.get("baseline"))
    lfm = _as_dict(aggregate.get("lfm_rerank"))
    paired = _as_dict(report.get("paired"))
    lfm_paired = _as_dict(paired.get("lfm_rerank"))
    medians = _as_dict(lfm_paired.get("median_deltas"))
    eligible = _as_dict(evidence.get("recall_at_50_eligible_counts"))
    gate = _as_dict(report.get("lfm_rerank_gate"))
    overall = _as_dict(report.get("overall_gate"))
    provenance = _as_dict(report.get("evaluation_provenance"))
    query_count = int(dataset.get("count") or 0)
    improved = _number(lfm_paired.get("improved_ndcg_ratio"))
    regressed = _number(lfm_paired.get("regressed_ndcg_ratio"))
    unchanged = None
    if improved is not None and regressed is not None:
        unchanged = max(0.0, 1.0 - improved - regressed)
    return {
        "query_count": query_count,
        "ru_or_ru_en_count": int(dataset.get("ru_or_ru_en_count") or 0),
        "production_real_count": int(dataset.get("production_real_count") or 0),
        "provenance_notice": str(dataset.get("provenance_notice") or ""),
        "production_gate_mode": bool(provenance.get("production_gate")),
        "baseline": {
            "hit_at_3": _number(baseline.get("hit@3")),
            "recall_at_10": _number(baseline.get("recall@10")),
            "ndcg_at_10": _number(baseline.get("ndcg@10")),
        },
        "lfm": {
            "hit_at_3": _number(lfm.get("hit@3")),
            "recall_at_10": _number(lfm.get("recall@10")),
            "ndcg_at_10": _number(lfm.get("ndcg@10")),
        },
        "median_ndcg_delta": _number(medians.get("ndcg@10")),
        "mean_ndcg_delta": _number(
            _as_dict(lfm_paired.get("mean_deltas")).get("ndcg@10")
        ),
        "improved_ratio": improved,
        "regressed_ratio": regressed,
        "unchanged_ratio": unchanged,
        "recall_at_50_eligible": int(eligible.get("lfm_rerank") or 0),
        "recall_at_50_complete": (
            int(eligible.get("lfm_rerank") or 0) == query_count
            and query_count > 0
        ),
        "gate_passed": overall.get("passed") is True and gate.get("passed") is True,
        "failed_checks": _checks_failed(gate),
    }


def _runtime_summary(runs: dict[str, Any]) -> dict[str, Any]:
    rows = runs.get("results")
    if not isinstance(rows, list):
        rows = []
    latencies: list[float] = []
    scored = 0
    failures = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        state = row.get("late_interaction")
        if not isinstance(state, dict):
            continue
        status = str(state.get("status") or "")
        if status == "scored":
            scored += 1
        else:
            failures += 1
        latency = _number(state.get("latency_ms"))
        if latency is not None:
            latencies.append(latency)
    return {
        "sample_count": len(latencies),
        "scored_count": scored,
        "failure_count": failures,
        "latency_ms": {
            "p50": round(median(latencies), 2) if latencies else None,
            "p95": (
                round(value, 2)
                if (value := _percentile(latencies, 0.95)) is not None
                else None
            ),
            "max": round(max(latencies), 2) if latencies else None,
        },
        "capture_complete": runs.get("complete") is True,
        "capture_failure_count": len(runs.get("failures") or []),
    }


def load_retained_lfm_evidence(
    report_root: Path,
    *,
    repository_id: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return the newest bounded diagnostic bundle, or an explicit unavailable state."""

    lfm_root = report_root / "lfm"
    if not lfm_root.is_dir():
        return {"status": "unavailable", "reason": "report_store_missing"}

    candidates = sorted(
        lfm_root.glob("paired-eval-*.json"),
        key=lambda path: path.stat().st_mtime if path.exists() else 0,
        reverse=True,
    )
    for report_path in candidates:
        loaded = _read_json(report_path, lfm_root)
        if loaded is None:
            continue
        report, artifact = loaded
        stem = report_path.stem.removeprefix("paired-eval-")
        build_label = stem.removesuffix("-diagnostic")
        runs_candidates = [
            lfm_root / f"paired-rankings-{build_label}-diagnostic-list.json",
            lfm_root / f"paired-rankings-{build_label}.json",
        ]
        runs_loaded = next(
            (
                result
                for candidate in runs_candidates
                if (result := _read_json(candidate, lfm_root)) is not None
            ),
            None,
        )
        # The rankings sidecar carries repository identity. Without it, an
        # otherwise valid global report cannot be shown under a selected
        # repository because its scope is unprovable.
        if repository_id is not None and runs_loaded is None:
            continue
        runs: dict[str, Any] = {}
        runs_artifact: dict[str, Any] | None = None
        if runs_loaded is not None:
            runs, runs_artifact = runs_loaded
            repository = _as_dict(runs.get("repository"))
            observed_repository_id = repository.get("id")
            if (
                repository_id is not None
                and observed_repository_id is not None
                and int(observed_repository_id) != repository_id
            ):
                continue

        observed_at = datetime.fromtimestamp(
            float(artifact["modified_timestamp"]),
            tz=timezone.utc,
        )
        current = now or datetime.now(timezone.utc)
        age_seconds = max(0, int((current - observed_at).total_seconds()))
        return {
            "status": "ready",
            "kind": (
                "diagnostic"
                if "diagnostic" in report_path.stem
                else "retained_evaluation"
            ),
            "artifact": {
                key: value
                for key, value in artifact.items()
                if key != "modified_timestamp"
            },
            "runs_artifact": (
                {
                    key: value
                    for key, value in runs_artifact.items()
                    if key != "modified_timestamp"
                }
                if runs_artifact
                else None
            ),
            "age_seconds": age_seconds,
            "stale": age_seconds > EVIDENCE_STALE_AFTER_SECONDS,
            "quality": _quality_summary(report),
            "runtime": _runtime_summary(runs),
        }

    return {"status": "unavailable", "reason": "no_parseable_evidence"}
