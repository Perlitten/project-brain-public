"""Transport-free insight value objects and evidence normalization."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any

INSIGHT_ENGINE_VERSION = "p0.1"
_VALID_SEVERITIES = {"critical", "warning", "info", "ready"}


@dataclass(slots=True)
class InsightCandidate:
    insight_type: str
    severity: str
    title: str
    summary: str
    evidence: list[dict[str, Any]]
    recommended_action: str | None = None
    confidence: str = "medium"
    dedupe_key: str | None = None
    source: str = "deterministic"
    source_model: str | None = None
    engine_version: str = INSIGHT_ENGINE_VERSION

    def __post_init__(self) -> None:
        self.insight_type = _safe_token(self.insight_type, fallback="general")[:100]
        self.severity = self.severity if self.severity in _VALID_SEVERITIES else "info"
        self.title = _clean_text(self.title, limit=255) or "Project Brain insight"
        self.summary = _clean_text(self.summary, limit=1600) or self.title
        self.recommended_action = _clean_text(self.recommended_action, limit=1200) if self.recommended_action else None
        self.confidence = _safe_token(self.confidence, fallback="medium")[:50]
        self.source = _safe_token(self.source, fallback="deterministic")[:50]
        self.source_model = _clean_text(self.source_model, limit=128) if self.source_model else None
        self.evidence = [_clean_evidence(item) for item in self.evidence if isinstance(item, dict)]
        self.evidence = [item for item in self.evidence if item]
        self.dedupe_key = (
            _clean_text(self.dedupe_key, limit=255)
            if self.dedupe_key
            else _dedupe_key(
                self.insight_type,
                self.title,
                self.evidence,
            )
        )

    def to_record(self) -> dict[str, Any]:
        return asdict(self)

    def to_public_dict(self, *, insight_id: int | None = None, status: str = "new") -> dict[str, Any]:
        payload = self.to_record()
        payload["id"] = insight_id
        payload["status"] = status
        return payload


def _clean_text(value: Any, *, limit: int) -> str:
    if value is None:
        return ""
    text = str(value).replace("\x00", "").strip()
    text = re.sub(r"\s+", " ", text)
    return text[:limit]


def _safe_token(value: Any, *, fallback: str) -> str:
    text = _clean_text(value, limit=120).lower()
    text = re.sub(r"[^a-z0-9_.:-]+", "_", text).strip("_")
    return text or fallback


def _clean_evidence(item: dict[str, Any]) -> dict[str, Any]:
    label = _clean_text(item.get("label") or item.get("name") or item.get("key"), limit=120)
    value = item.get("value")
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)[:800]
    else:
        value = _clean_text(value, limit=800)
    if not label or value in (None, ""):
        return {}
    return {"label": label, "value": value}


def _dedupe_key(insight_type: str, title: str, evidence: list[dict[str, Any]]) -> str:
    raw = json.dumps(
        {
            "type": insight_type,
            "title": title.lower(),
            "evidence": evidence[:4],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return f"{insight_type}:{digest}"


def _safe_count(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
