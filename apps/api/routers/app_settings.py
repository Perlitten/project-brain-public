"""Operator settings: alerts, automation, search and indexing knobs.

Only the fields in :data:`FIELDS` are readable or writable here, each with its
own type and range. Secrets (bot token) are write-only: GET says
whether one is set, never what it is. Writes land in the checkout's ``.env``
(through ``brain.onboarding.envfile``) and on the live API settings; workers
and the scheduler read ``.env`` when they start.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional, Union
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field as PydanticField

from apps.api.auth import require_api_key, require_scope
from brain.config.settings import settings
from brain.onboarding.envfile import setup_env_path, update_env_file

router = APIRouter(prefix="/api/settings", tags=["settings"], dependencies=[Depends(require_api_key)])

Kind = Literal["bool", "int", "float", "text", "url", "secret"]


@dataclass(frozen=True)
class SettingField:
    key: str
    section: str
    label: str
    kind: Kind
    help: str
    min: Optional[float] = None
    max: Optional[float] = None
    pattern: Optional[str] = None
    clearable: bool = False


SECTIONS: Dict[str, str] = {
    "telegram": "Telegram alerts",
    "automation": "Background checks",
    "search": "Search & context packs",
    "indexing": "Indexing & workers",
}

FIELDS: List[SettingField] = [
    SettingField("TELEGRAM_ALERTS_ENABLED", "telegram", "Send alerts", "bool", "Self-diagnosis, maintenance and deep-context findings go to the chat below."),
    SettingField("TELEGRAM_ALERT_BOT_TOKEN", "telegram", "Bot token", "secret", "From @BotFather → /newbot. Write-only.", clearable=True),
    SettingField("TELEGRAM_ALERT_CHAT_ID", "telegram", "Chat ID", "text", "Numeric id of the chat or channel (e.g. -1001234567890) or @channel.", pattern=r"-?\d{1,20}|@[A-Za-z0-9_]{4,64}", clearable=True),
    SettingField("TELEGRAM_ALERT_COOLDOWN_SECONDS", "telegram", "Repeat cooldown, s", "int", "The same findings are not re-sent within this window.", min=60, max=7 * 24 * 3600),
    SettingField("BRAIN_TELEGRAM_DEFAULT_REPO", "telegram", "Default repository for the bot", "text", "Repository the Telegram bridge uses when a message names none.", clearable=True),
    SettingField("SELF_DIAGNOSIS_ENABLED", "automation", "Scheduled self-diagnosis", "bool", "Brain checks its own health on a schedule and reports findings."),
    SettingField("SELF_DIAGNOSIS_USE_LLM", "automation", "Summarize diagnosis with the LLM", "bool", "Off = deterministic probes only, no model calls."),
    SettingField("PROACTIVE_INSIGHTS_ENABLED", "automation", "Proactive insights", "bool", "Scheduled scan for risks and drift in the indexed code."),
    SettingField("PROACTIVE_INSIGHTS_LLM_ENABLED", "automation", "Insights use the LLM", "bool", "Let the model phrase insights; costs model calls."),
    SettingField("NIGHTLY_DEEP_MAINTENANCE_ENABLED", "automation", "Nightly deep maintenance", "bool", "Nightly re-scoring and quality checks of the index."),
    SettingField("RETRIEVAL_USE_LLM_RERANK", "search", "LLM rerank", "bool", "Better ordering of search results, slower and costs model calls."),
    SettingField("RETRIEVAL_USE_LLM_CLASSIFICATION", "search", "LLM query classification", "bool", "Let the model decide what kind of question it is."),
    SettingField("RETRIEVAL_PACK_LIMIT", "search", "Files per context pack", "int", "Upper bound on files picked for one briefing.", min=1, max=50),
    SettingField("CONTEXT_PACK_SKIP_PLAN_LLM", "search", "Skip the planning call", "bool", "Faster briefings without the model’s plan step."),
    SettingField("INDEX_FILE_CONCURRENCY", "indexing", "Files in parallel", "int", "Higher is faster on big machines.", min=1, max=32),
    SettingField("INDEX_PROVIDER_CONCURRENCY", "indexing", "Model calls in parallel", "int", "Keep low on rate-limited providers.", min=1, max=16),
    SettingField("INDEX_EMBEDDING_BATCH_SIZE", "indexing", "Embedding batch size", "int", "Texts per embeddings request.", min=1, max=256),
    SettingField("WORKER_JOB_TIMEOUT_SECONDS", "indexing", "Job timeout, s", "int", "A background job is stopped after this long.", min=60, max=6 * 3600),
]
BY_KEY: Dict[str, SettingField] = {f.key: f for f in FIELDS}
SETTINGS_KEYS = frozenset(BY_KEY)


def _public(f: SettingField) -> Dict[str, Any]:
    raw = getattr(settings, f.key, None)
    out: Dict[str, Any] = {
        "key": f.key,
        "label": f.label,
        "kind": f.kind,
        "help": f.help,
        "clearable": f.clearable,
        # Also passed in the process environment (e.g. docker compose): that
        # value wins over .env on the next restart.
        "from_env": f.key in os.environ,
    }
    if f.min is not None:
        out["min"] = f.min
    if f.max is not None:
        out["max"] = f.max
    if f.kind == "secret":
        out["set"] = bool(str(raw or "").strip())
        out["value"] = None
    else:
        out["value"] = raw
    return out


def _safe_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    p = urlparse(url)
    if not p.hostname:
        return None
    host = f"{p.hostname}:{p.port}" if p.port else p.hostname
    return f"{p.scheme}://{host}{p.path}"


@router.get("", dependencies=[Depends(require_scope("setup:read"))])
async def get_settings() -> Dict[str, Any]:
    sections = [
        {"id": sid, "title": title, "fields": [_public(f) for f in FIELDS if f.section == sid]}
        for sid, title in SECTIONS.items()
    ]
    return {
        "sections": sections,
        # Shown, edited on /setup.
        "models": {
            "llm_provider": settings.DEFAULT_LLM_PROVIDER,
            "llm_model": getattr(settings, "LLM_MODEL", None),
            "summarizer_model": getattr(settings, "SUMMARIZER_MODEL", None),
            "embedding_provider": settings.DEFAULT_EMBEDDING_PROVIDER,
            "embedding_model": getattr(settings, "EMBEDDING_MODEL", None),
            "embedding_dimension": getattr(settings, "EMBEDDING_DIMENSION", None),
            "llm_base_url": _safe_url(getattr(settings, "LLM_BASE_URL", None)),
            "embedding_base_url": _safe_url(getattr(settings, "EMBEDDING_BASE_URL", None)),
        },
        "repo_path": settings.TARGET_REPO_PATH,
        "env_file": {"exists": setup_env_path().exists()},
    }


Value = Union[bool, int, float, str, None]


class SettingsPatch(BaseModel):
    values: Dict[str, Value] = PydanticField(default_factory=dict)
    clear: List[str] = PydanticField(default_factory=list)


def _coerce(f: SettingField, value: Value) -> tuple[str, Any]:
    """(.env text, live value) for one field, or ValueError."""
    if f.kind == "bool":
        if isinstance(value, bool):
            b = value
        elif isinstance(value, str) and value.lower() in ("true", "false"):
            b = value.lower() == "true"
        else:
            raise ValueError(f"{f.label}: on or off")
        return ("true" if b else "false"), b
    if f.kind in ("int", "float"):
        if isinstance(value, bool) or value is None:
            raise ValueError(f"{f.label}: a number")
        try:
            n: float = int(value) if f.kind == "int" else float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{f.label}: a {'whole ' if f.kind == 'int' else ''}number") from None
        if f.kind == "int" and isinstance(value, float) and not value.is_integer():
            raise ValueError(f"{f.label}: a whole number")
        if (f.min is not None and n < f.min) or (f.max is not None and n > f.max):
            raise ValueError(f"{f.label}: between {f.min:g} and {f.max:g}")
        return str(n), n
    if not isinstance(value, str):
        raise ValueError(f"{f.label}: text")
    text = value.strip()
    if not text:
        raise ValueError(f"{f.label}: empty — use clear to remove it")
    if len(text) > (4096 if f.kind == "secret" else 500):
        raise ValueError(f"{f.label}: too long")
    if f.kind == "secret" and any(ch.isspace() for ch in text):
        raise ValueError(f"{f.label}: must not contain spaces")
    if f.kind == "url":
        p = urlparse(text)
        if p.scheme not in ("http", "https") or not p.hostname:
            raise ValueError(f"{f.label}: a full http(s):// address")
        if p.username or p.password or p.query or p.fragment:
            raise ValueError(f"{f.label}: no credentials, query or fragment in the URL")
        text = text.rstrip("/")
    if f.pattern and not re.fullmatch(f.pattern, text):
        raise ValueError(f"{f.label}: unexpected format")
    return text, text


@router.patch("", dependencies=[Depends(require_scope("setup:write"))])
async def patch_settings(body: SettingsPatch) -> Dict[str, Any]:
    updates: Dict[str, str] = {}
    live: Dict[str, Any] = {}
    try:
        for key, value in body.values.items():
            f = BY_KEY.get(key)
            if f is None:
                raise ValueError(f"{key} is not an editable setting")
            updates[key], live[key] = _coerce(f, value)
        for key in body.clear:
            f = BY_KEY.get(key)
            if f is None or not f.clearable:
                raise ValueError(f"{key} cannot be cleared")
            if key in updates:
                raise ValueError(f"{key}: set and clear at once")
            updates[key], live[key] = "", None
        if not updates:
            raise ValueError("nothing to change")
        update_env_file(setup_env_path(), updates, extra_keys=SETTINGS_KEYS)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"could not write .env: {type(exc).__name__}") from exc
    for key, value in live.items():
        setattr(settings, key, value)
    notes = ["Saved to the server’s .env and applied to the API. Restart the workers and scheduler so background jobs use it too."]
    overridden = sorted(k for k in updates if k in os.environ)
    if overridden:
        notes.append(
            "Also set in the server’s process environment (e.g. docker compose): "
            + ", ".join(overridden)
            + " — change it there too, or the old value returns on restart."
        )
    # Keys only, never values: secrets are write-only.
    return {"applied": sorted(updates), "notes": notes}


@router.post("/test/telegram", dependencies=[Depends(require_scope("setup:write"))])
async def test_telegram() -> Dict[str, Any]:
    """Send one test message with the saved bot token and chat id."""
    from brain.alerts.telegram import _send

    result = await _send("Project Brain: test message from Settings. Alerts will arrive in this chat.")
    return {
        "status": result.get("status"),
        "detail": result.get("reason") or result.get("error_type"),
        "enabled": bool(settings.TELEGRAM_ALERTS_ENABLED),
    }
