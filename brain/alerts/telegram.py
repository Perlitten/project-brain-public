"""Deduplicated Telegram delivery for self-diagnosis findings."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

import httpx
from loguru import logger

from brain.config.settings import settings
from brain.database.session import redis_client
from brain.version import build_info

ALERT_STATE_KEY = "brain:self-diagnosis:telegram-state"
LAST_DELIVERY_KEY = "brain:self-diagnosis:last-delivery"
TELEGRAM_MAX_TEXT = 3900


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _actionable(insights: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        item
        for item in insights
        if str(item.get("severity") or "").lower() in {"critical", "warning"}
        and str(item.get("status") or "new").lower() in {"new", "accepted"}
    ]


def _fingerprint(insights: list[dict[str, Any]]) -> str:
    material = [
        {
            "dedupe_key": item.get("dedupe_key"),
            "severity": item.get("severity"),
            "summary": item.get("summary"),
        }
        for item in sorted(insights, key=lambda value: str(value.get("dedupe_key") or ""))
    ]
    return hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _message(
    findings: list[dict[str, Any]],
    *,
    generated_at: str,
    llm: dict[str, Any],
    recovery: bool = False,
) -> str:
    build = build_info()
    if recovery:
        return (
            "🧠 Project Brain восстановился\n"
            "Активные critical/warning findings больше не наблюдаются.\n"
            f"Проверка: {generated_at}\n"
            f"Build: {build.get('build_sha', 'unknown')[:12]}"
        )

    critical = sum(1 for item in findings if item.get("severity") == "critical")
    warning = sum(1 for item in findings if item.get("severity") == "warning")
    lines = [
        "🧠 Project Brain: требуется внимание",
        f"Critical: {critical} · Warning: {warning}",
    ]
    for item in findings[:8]:
        marker = "🔴" if item.get("severity") == "critical" else "🟠"
        title = " ".join(str(item.get("title") or "Finding").split())[:180]
        summary = " ".join(str(item.get("summary") or "").split())[:420]
        action = " ".join(str(item.get("recommended_action") or "").split())[:360]
        lines.append(f"\n{marker} {title}\n{summary}")
        if action:
            lines.append(f"Действие: {action}")
    if len(findings) > 8:
        lines.append(f"\nЕщё findings: {len(findings) - 8}")
    lines.extend(
        [
            f"\nLLM: {llm.get('status', 'unknown')} · {llm.get('model', 'unknown')}",
            f"Проверка: {generated_at}",
            f"Build: {build.get('build_sha', 'unknown')[:12]}",
        ]
    )
    return "\n".join(lines)[:TELEGRAM_MAX_TEXT]


async def _send(text: str) -> dict[str, Any]:
    token = settings.TELEGRAM_ALERT_BOT_TOKEN
    chat_id = settings.TELEGRAM_ALERT_CHAT_ID
    if not token or not chat_id:
        return {"status": "disabled", "reason": "telegram_credentials_missing"}
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=settings.TELEGRAM_ALERT_TIMEOUT_S) as client:
            response = await client.post(
                url,
                data={
                    "chat_id": chat_id,
                    "text": text,
                    "disable_web_page_preview": "true",
                },
            )
            response.raise_for_status()
    except Exception as exc:  # never log the URL: it contains the bot token
        logger.warning(f"Telegram alert delivery failed: {type(exc).__name__}")
        return {
            "status": "failed",
            "error_type": type(exc).__name__,
        }
    return {"status": "sent", "http_status": response.status_code}


async def deliver_maintenance_alert(
    *,
    repo_path: str,
    dense_verification: dict[str, Any],
    late_sync: dict[str, Any],
    quality_probes: list[dict[str, Any]],
    repaired_embeddings: int,
) -> dict[str, Any]:
    """Send one sanitized nightly maintenance receipt to the owner."""
    if not settings.TELEGRAM_ALERTS_ENABLED:
        return {"status": "disabled", "reason": "TELEGRAM_ALERTS_ENABLED=false"}
    coverage = dense_verification.get("pgvector_coverage_pct")
    scored = sum(1 for probe in quality_probes if probe.get("passed"))
    build = build_info().get("build_sha", "unknown")
    lines = [
        "🧠 Project Brain: ночное обслуживание завершено",
        f"Репозиторий: {repo_path}",
        (
            "Dense: "
            f"{dense_verification.get('chunks_with_current_embeddings', 0)}/"
            f"{dense_verification.get('total_eligible_chunks', 0)}"
            f" · coverage {coverage if coverage is not None else 'unknown'}%"
            f" · repaired {repaired_embeddings}"
        ),
        (
            "LFM: "
            f"{late_sync.get('verified_documents', 0)} документов"
            f" · {late_sync.get('index_revision') or 'revision unavailable'}"
            f" · changed {late_sync.get('accepted', 0)}"
            f" · deleted {late_sync.get('deleted', 0)}"
        ),
        f"Deep relevance probes: {scored}/{len(quality_probes)} passed",
        f"Build: {str(build)[:12]}",
    ]
    return await _send("\n".join(lines)[:TELEGRAM_MAX_TEXT])


async def deliver_deep_context_alert(
    *,
    repo_path: str,
    context_pack_id: int | None,
    critic_status: str,
    late_status: str,
    retrieved_files: int,
) -> dict[str, Any]:
    """Notify that an asynchronous deep context pack is ready.

    The task text is deliberately omitted; Telegram receives only operational
    metadata and the database artifact id.
    """
    if not settings.TELEGRAM_ALERTS_ENABLED:
        return {"status": "disabled", "reason": "TELEGRAM_ALERTS_ENABLED=false"}
    build = build_info().get("build_sha", "unknown")
    lines = [
        "🧠 Project Brain: Deep context готов",
        f"Репозиторий: {repo_path}",
        f"Context pack: {context_pack_id if context_pack_id is not None else 'unknown'}",
        f"LFM: {late_status or 'unknown'}",
        f"Critic: {critic_status or 'unknown'}",
        f"Файлов: {retrieved_files}",
        f"Build: {str(build)[:12]}",
    ]
    return await _send("\n".join(lines)[:TELEGRAM_MAX_TEXT])


async def deliver_diagnosis_alert(
    insights: list[dict[str, Any]],
    *,
    generated_at: str,
    llm: dict[str, Any],
) -> dict[str, Any]:
    """Send changed findings, suppress repeats, and announce recovery."""
    if not settings.TELEGRAM_ALERTS_ENABLED:
        return {"status": "disabled", "reason": "TELEGRAM_ALERTS_ENABLED=false"}

    now = _now()
    findings = _actionable(insights)
    raw_state = await redis_client.get(ALERT_STATE_KEY)
    try:
        state = json.loads(raw_state) if raw_state else {}
    except json.JSONDecodeError:
        state = {}
    previous_fingerprint = state.get("fingerprint")
    previous_active = bool(state.get("active"))
    last_sent = _parse_datetime(state.get("last_sent_at"))

    if findings:
        fingerprint = _fingerprint(findings)
        cooldown_elapsed = (
            last_sent is None
            or (now - last_sent).total_seconds() >= settings.TELEGRAM_ALERT_COOLDOWN_SECONDS
        )
        if fingerprint == previous_fingerprint and not cooldown_elapsed:
            result = {
                "status": "suppressed",
                "reason": "unchanged_within_cooldown",
                "finding_count": len(findings),
                "fingerprint": fingerprint,
            }
        else:
            result = await _send(
                _message(findings, generated_at=generated_at, llm=llm)
            )
            if result["status"] == "sent":
                state = {
                    "active": True,
                    "fingerprint": fingerprint,
                    "active_keys": [item.get("dedupe_key") for item in findings],
                    "last_sent_at": now.isoformat(),
                }
            result.update(
                {
                    "finding_count": len(findings),
                    "fingerprint": fingerprint,
                }
            )
    elif previous_active:
        result = await _send(
            _message([], generated_at=generated_at, llm=llm, recovery=True)
        )
        if result["status"] == "sent":
            state = {
                "active": False,
                "fingerprint": None,
                "active_keys": [],
                "last_sent_at": now.isoformat(),
            }
        result["recovery"] = True
        result["finding_count"] = 0
    else:
        result = {"status": "quiet", "finding_count": 0}
        state = {
            "active": False,
            "fingerprint": None,
            "active_keys": [],
            "last_sent_at": state.get("last_sent_at"),
        }

    state["last_evaluated_at"] = now.isoformat()
    state["last_delivery"] = result
    ttl = max(settings.SELF_DIAGNOSIS_RESULT_TTL_SECONDS, 60)
    pipe = redis_client.pipeline()
    pipe.set(ALERT_STATE_KEY, json.dumps(state), ex=ttl)
    pipe.set(
        LAST_DELIVERY_KEY,
        json.dumps({**result, "evaluated_at": now.isoformat()}),
        ex=ttl,
    )
    await pipe.execute()
    return result
