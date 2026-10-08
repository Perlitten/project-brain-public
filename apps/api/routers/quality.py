"""Read-only source-backed quality evidence for the web dashboard."""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from apps.api.auth import require_api_key, require_scope
from brain.config.paths import get_repo_root

router = APIRouter(
    prefix="/api/web",
    tags=["web-quality"],
    dependencies=[Depends(require_api_key), Depends(require_scope("core:read"))],
)

_ARTIFACT = "eval/quality/owner-benchmark-20261008.json"
_REQUIRED = ("schema_version", "generated_at", "source", "search", "context", "acceptance")


def _quality_path() -> Path:
    root = get_repo_root().resolve()
    candidate = (root / _ARTIFACT).resolve()
    if root not in candidate.parents:
        raise HTTPException(status_code=503, detail="Quality evidence path is invalid")
    return candidate


def load_quality_evidence() -> dict[str, Any]:
    path = _quality_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail="Quality evidence is unavailable") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=503, detail="Quality evidence is malformed") from exc
    if not isinstance(payload, dict) or any(key not in payload for key in _REQUIRED):
        raise HTTPException(status_code=503, detail="Quality evidence schema is incomplete")
    if type(payload.get("schema_version")) is not int or payload.get("schema_version") != 1 or not isinstance(payload.get("source"), dict):
        raise HTTPException(status_code=503, detail="Quality evidence schema is unsupported")
    try:
        _validate_quality(payload)
    except (TypeError, ValueError, KeyError):
        raise HTTPException(status_code=503, detail="Quality evidence schema is malformed") from None
    return payload


def _validate_quality(payload: dict[str, Any]) -> None:
    if payload.get("historical") is not True:
        raise ValueError("artifact must be historical")
    timestamp = payload.get("generated_at")
    if timestamp is not None:
        if not isinstance(timestamp, str):
            raise ValueError("timestamp type")
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timestamp timezone")
    source = payload["source"]
    expected_lengths = {"build_sha": 40, "source_digest": 64, "corpus_sha256": 64}
    for key, expected_length in expected_lengths.items():
        value = source.get(key)
        if not isinstance(value, str) or len(value) != expected_length or any(c not in "0123456789abcdef" for c in value.lower()):
            raise ValueError(key)
    _count(source, "search_sample_count")
    _count(source, "context_fixture_count")
    provenance = source["provenance"]
    if not isinstance(provenance, list) or not provenance:
        raise ValueError("provenance")
    for item in provenance:
        if not isinstance(item, dict) or not isinstance(item.get("artifact"), str):
            raise ValueError("provenance artifact")
        for field, length in (("sha256", 64), ("measured_build_sha", 40)):
            if not isinstance(item.get(field), str) or len(item[field]) != length or any(c not in "0123456789abcdef" for c in item[field]):
                raise ValueError("provenance hash")
    search = payload["search"]
    context = payload["context"]
    _count(context, "normal_count")
    if context["normal_count"] > source["context_fixture_count"] or not isinstance(context["adversarial_status"], str):
        raise ValueError("context fixtures")
    limits = payload["quality_limits"]
    _count(limits, "native_tasks_passed")
    _count(limits, "native_task_count")
    if limits["native_tasks_passed"] > limits["native_task_count"]:
        raise ValueError("task count")
    _number(limits, "native_precision", 0, 1)
    _number(limits, "native_recall", 0, 1)
    for key in ("hit1_any_pct", "hit3_any_pct", "hit5_any_pct"):
        _number(search, key, 0, 100)
    _number(search, "mrr_any", 0, 1)
    for key in ("mean_recall", "mean_mrr", "mean_fixture_noise_ratio"):
        _number(context, key, 0, 1)
    for key in ("latency_p50_ms", "latency_p95_ms", "latency_max_ms"):
        _number(search, key, 0, None)
    for key in ("latency_p50_ms", "latency_p95_ms"):
        _number(context, key, 0, None)
    if not isinstance(payload.get("acceptance_scope"), list) or not all(isinstance(v, str) for v in payload["acceptance_scope"]):
        raise ValueError("acceptance_scope")


def _count(mapping: dict[str, Any], key: str) -> None:
    if type(mapping[key]) is not int or mapping[key] < 0:
        raise ValueError(key)


def _number(mapping: dict[str, Any], key: str, low: float, high: float | None) -> None:
    value = mapping[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < low:
        raise ValueError(key)
    if high is not None and value > high:
        raise ValueError(key)


@router.get("/quality")
async def web_quality() -> dict[str, Any]:
    return load_quality_evidence()
