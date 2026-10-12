"""Telegram alerts for the owner: new problems, critical reminders, fixes.

A message is worth a buzz only when it tells the owner something they don't
know yet and what to do about it. So:

- each finding is announced once, when it first appears (or turns critical);
- only critical findings are repeated, at most once per cooldown;
- a problem that goes away is reported as fixed;
- one-off events (a failed job) are announced once and then forgotten;
- LLM observations are not pushed: they are reworded on every run, so they
  would read as new each time. They stay on the dashboard's Findings page;
- a healthy nightly maintenance run sends nothing.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Callable

import httpx
from loguru import logger

from brain.config.settings import settings
from brain.database.session import redis_client

ALERT_STATE_KEY = "brain:self-diagnosis:telegram-state"
LAST_DELIVERY_KEY = "brain:self-diagnosis:last-delivery"
TELEGRAM_MAX_TEXT = 3900
MAX_LISTED = 6

# Findings that describe something that happened, not a state that can recover.
EVENT_PREFIXES = ("worker_health:failed_job:", "automation_trigger:", "indexing_health:latest_failed:")

_ICON = {"critical": "🔴", "warning": "🟠"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _text(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def _link(path: str) -> str:
    base = (settings.BRAIN_WEB_URL or "").strip().rstrip("/")
    return f"{base}{path}" if base else ""


def _evidence(item: dict[str, Any], label: str) -> Any:
    for entry in item.get("evidence") or []:
        if isinstance(entry, dict) and entry.get("label") == label:
            return entry.get("value")
    return None


def _is_event(key: str) -> bool:
    return key.startswith(EVENT_PREFIXES)


# ---------------------------------------------------------------------------
# What each finding means, in plain words


class _Card:
    def __init__(self, title: str, detail: str = "", action: str = "", path: str = "/insights"):
        self.title = title
        self.detail = detail
        self.action = action
        self.path = path


def _job_card(item: dict[str, Any]) -> _Card:
    job_type = _evidence(item, "job_type") or "background"
    error = _evidence(item, "error")
    job_id = _evidence(item, "job_id")
    detail = f"Error: {_text(error, 300)}" if error and error != "failed" else ""
    if job_id:
        detail = f"{detail} (job {job_id})".strip()
    return _Card(
        f"Job failed: {job_type}",
        detail,
        "Check the error in the job log and re-run once the cause is fixed.",
        "/logs",
    )


def _trigger_card(item: dict[str, Any]) -> _Card:
    reference = str(_evidence(item, "reference") or "")
    job_type = reference.split(":", 1)[0] if ":" in reference else ""
    return _Card(
        f"Job failed: {job_type}" if job_type else "An external automation reported an error",
        f"Error: {_text(item.get('summary'), 300)}",
        "Check the error in the job log and re-run once the cause is fixed.",
        "/logs",
    )


def _services_card(item: dict[str, Any]) -> _Card:
    names = [str(entry.get("label")) for entry in item.get("evidence") or [] if isinstance(entry, dict)]
    return _Card(
        f"Down: {', '.join(names) or 'storage'}",
        "While the service is down, search, context and background jobs do not work.",
        "Bring the service back: docker compose ps, then docker compose logs <service>.",
        "/logs",
    )


def _recall_card(item: dict[str, Any]) -> _Card:
    recall = _evidence(item, "avg_recall")
    floor = _evidence(item, "minimum_avg_recall")
    detail = "Search finds fewer of the expected files than allowed"
    if recall is not None and floor is not None:
        detail += f": recall {recall}, floor {floor}"
    return _Card(
        "Search quality below the floor",
        detail + ".",
        "Open the benchmark, see which files were missed, fix the index and re-run the benchmark.",
        "/quality",
    )


def _embeddings_card(item: dict[str, Any]) -> _Card:
    parts = []
    for label, word in (("missing_embeddings", "missing"), ("stale_embeddings", "stale"),
                        ("incompatible_embeddings", "incompatible")):
        try:
            count = int(_evidence(item, label) or 0)
        except (TypeError, ValueError):
            count = 0
        if count:
            parts.append(f"{count} {word}")
    return _Card(
        "Part of the code is invisible to semantic search",
        f"Chunks: {', '.join(parts)}." if parts else "",
        "Run the embedding backfill and wait for 100% coverage.",
        "/indexing",
    )


def _freshness_card(item: dict[str, Any]) -> _Card:
    repos = [
        f"{PurePosixPath(str(entry.get('label'))).name}: {entry.get('value')}"
        for entry in (item.get("evidence") or [])[:3]
        if isinstance(entry, dict)
    ]
    return _Card(
        "Index does not match the source",
        "; ".join(repos),
        "Re-index the repository and check embeddings.",
        "/indexing",
    )


def _drift_card(item: dict[str, Any]) -> _Card:
    title = str(item.get("title") or "")
    rule = title.split(": ", 1)[1] if ": " in title else title
    where = _evidence(item, "file_path")
    line = _evidence(item, "line_number")
    module = _evidence(item, "imported_module")
    detail = f"{where}:{line}" if where and line else str(where or "")
    if module:
        detail = f"{detail} imports {module}".strip()
    return _Card(f"Architecture boundary violated: {rule}", detail, _text(item.get("recommended_action"), 300))


def _count_card(title: str, label: str, noun: tuple[str, str], action: str, path: str) -> Callable:
    def card(item: dict[str, Any]) -> _Card:
        try:
            n = int(_evidence(item, label) or 0)
        except (TypeError, ValueError):
            n = 0
        return _Card(title, f"{n} {_plural(n, *noun)}." if n else "", action, path)

    return card


def _static(title: str, detail: str, action: str, path: str = "/insights") -> Callable:
    return lambda item: _Card(title, detail, action, path)


_CARDS: list[tuple[str, Callable[[dict[str, Any]], _Card]]] = [
    ("worker_health:failed_job:", _job_card),
    ("automation_trigger:", _trigger_card),
    ("service_health:core_unhealthy", _services_card),
    ("retrieval_quality:recall_below_floor", _recall_card),
    ("retrieval_health:embedding_inventory_not_clean", _embeddings_card),
    ("repository_freshness:not_current", _freshness_card),
    ("architectural_drift:", _drift_card),
    (
        "worker_health:stale_durable_jobs",
        _static(
            "Background jobs are stuck",
            "Queued jobs have not moved for longer than the recovery window.",
            "Restart the worker: on start it returns the jobs to the queue.",
            "/logs",
        ),
    ),
    (
        "automation_health:scheduler_stale",
        lambda item: _Card(
            "Scheduled jobs are not running",
            f"Overdue: {_evidence(item, 'stale_jobs')}." if _evidence(item, "stale_jobs") else "",
            "Check that the worker runs with SCHEDULER_ENABLED=true. Meanwhile use Run now in Settings > Scheduler.",
            "/settings",
        ),
    ),
    (
        "harness_lifecycle:stale_tasks",
        _count_card(
            "Agent tasks are stuck",
            "stale_task_count",
            ("task timed out", "tasks timed out"),
            "Run task reconciliation and close the orphaned ones.",
            "/runs",
        ),
    ),
    (
        "graph_identity:legacy_unscoped_nodes",
        _count_card(
            "Graph nodes without a repository remain",
            "legacy_unscoped_nodes",
            ("node", "nodes"),
            "Re-index the active repositories, then delete the verified remainder.",
            "/graph",
        ),
    ),
    (
        "indexing_health:latest_failed:",
        lambda item: _Card(
            "Latest indexing run failed",
            f"Run {_evidence(item, 'run_id')}." if _evidence(item, "run_id") else "",
            "Check the error in the worker logs and re-run indexing (without clean).",
            "/indexing",
        ),
    ),
    (
        "indexing_health:no_source_inventory",
        _static("Index is empty", "Brain knows no files of the repository.", "Run indexing.", "/indexing"),
    ),
    (
        "retrieval_quality:evaluation_missing",
        _static("Search quality not measured", "No recent benchmark report.", "Run the search benchmark.", "/quality"),
    ),
    (
        "release_identity:unverifiable",
        _static(
            "Cannot verify which version is in production",
            "The API reports no source revision or digest.",
            "Redeploy with deploy/server_up.sh and check /api/version.",
        ),
    ),
    (
        "llm_diagnostics:stage_unavailable",
        lambda item: _Card(
            "LLM diagnostics unavailable",
            f"Error: {_text(_evidence(item, 'error'), 200)}." if _evidence(item, "error") else "",
            "Check the LLM provider. Deterministic checks keep running.",
            "/settings",
        ),
    ),
    (
        "llm_diagnostics:truncated_output",
        _static(
            "LLM diagnostics output truncated",
            "Part of the model output was dropped.",
            "Raise the model output limit and re-run self-diagnosis.",
            "/settings",
        ),
    ),
]


def _card(item: dict[str, Any]) -> _Card:
    key = str(item.get("dedupe_key") or "")
    for prefix, build in _CARDS:
        if key.startswith(prefix):
            return build(item)
    return _Card(
        _text(item.get("title") or "Problem", 180),
        _text(item.get("summary"), 300),
        _text(item.get("recommended_action"), 300),
    )


def _render(item: dict[str, Any], *, prefix: str = "") -> str:
    card = _card(item)
    lines = [f"{prefix or _ICON.get(str(item.get('severity')), '🟠')} {card.title}"]
    if card.detail:
        lines.append(card.detail)
    if card.action:
        lines.append(f"→ {card.action}")
    link = _link(card.path)
    if link:
        lines.append(link)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Which findings to push


def _pushable(insights: list[dict[str, Any]]) -> list[dict[str, Any]]:
    findings = [
        item
        for item in insights
        if str(item.get("severity") or "").lower() in {"critical", "warning"}
        and str(item.get("status") or "new").lower() in {"new", "accepted"}
        and str(item.get("source") or "deterministic").lower() != "llm"
        and item.get("dedupe_key")
    ]
    # A failed job routes itself to self-diagnosis, so the same failure arrives
    # twice: as the trigger and as a failed job. Keep the one with the job id.
    failed_jobs = {
        f"{_evidence(item, 'job_type')}:{_evidence(item, 'job_id')}"
        for item in findings
        if str(item["dedupe_key"]).startswith("worker_health:failed_job:")
    }
    return [
        item
        for item in findings
        if not (
            str(item["dedupe_key"]).startswith("automation_trigger:")
            and str(_evidence(item, "reference") or "") in failed_jobs
        )
    ]


def _load_active(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    active = state.get("active")
    if isinstance(active, dict):
        return {str(key): value for key, value in active.items() if isinstance(value, dict)}
    # State written before per-finding tracking: treat its findings as already
    # announced, so an upgrade doesn't repeat them.
    since = state.get("last_sent_at")
    return {
        str(key): {"severity": "warning", "notified_at": since, "since": since}
        for key in state.get("active_keys") or []
        if key
    }


def _diagnosis_message(
    new: list[dict[str, Any]],
    reminders: list[tuple[dict[str, Any], float]],
    fixed: list[str],
    still_open: int,
) -> str:
    blocks: list[str] = []
    if new:
        n = len(new)
        worst = "🔴" if any(item.get("severity") == "critical" for item in new) else "🟠"
        blocks.append(f"{worst} Project Brain: {n} {_plural(n, 'new problem', 'new problems')}")
        blocks.extend(_render(item) for item in new[:MAX_LISTED])
        if n > MAX_LISTED:
            blocks.append(f"…and {n - MAX_LISTED} more on the dashboard.")
    if reminders:
        if not new:
            blocks.append("⏰ Project Brain: critical problems still unresolved")
        for item, hours in reminders[:MAX_LISTED]:
            blocks.append(_render(item, prefix=f"⏰ open {round(hours)} h ·"))
    if fixed:
        if not new and not reminders:
            blocks.append("✅ Project Brain: all clear again" if not still_open else "✅ Project Brain: fixed")
        blocks.append("\n".join(f"Resolved: {title}" for title in fixed[:MAX_LISTED]))
    if still_open and (new or fixed):
        link = _link("/insights")
        blocks.append(f"Still open, already reported: {still_open}" + (f" - {link}" if link else ""))
    return "\n\n".join(blocks)[:TELEGRAM_MAX_TEXT]


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
    """Tell the owner about a nightly run only when it left something to fix.

    A clean run is silent: failures of the run itself already reach the owner
    through self-diagnosis, and a nightly "all fine" teaches them to ignore the chat.
    """
    if not settings.TELEGRAM_ALERTS_ENABLED:
        return {"status": "disabled", "reason": "TELEGRAM_ALERTS_ENABLED=false"}
    total = int(dense_verification.get("total_eligible_chunks") or 0)
    current = int(dense_verification.get("chunks_with_current_embeddings") or 0)
    passed = sum(1 for probe in quality_probes if probe.get("passed"))
    problems = []
    if current < total:
        problems.append(
            f"{total - current} of {total} chunks lack current embeddings; semantic search cannot see them."
        )
    if passed < len(quality_probes):
        problems.append(f"Relevance probes: {passed} of {len(quality_probes)} passed.")
    if not problems:
        return {"status": "quiet", "reason": "healthy", "repaired_embeddings": repaired_embeddings}
    link = _link("/indexing")
    lines = [
        f"🟠 Nightly maintenance of {PurePosixPath(repo_path).name}: needs attention",
        *problems,
        "→ Run the embedding backfill and check the index.",
    ]
    if link:
        lines.append(link)
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
    pack = f"#{context_pack_id}" if context_pack_id is not None else ""
    lines = [
        f"🧠 Deep context {pack} ready".replace("  ", " "),
        f"{PurePosixPath(repo_path).name}: {retrieved_files} "
        f"{_plural(retrieved_files, 'file', 'files')}",
    ]
    critic = {"PARTIAL_CONTEXT": "context incomplete", "LOW_CONFIDENCE": "low confidence"}.get(critic_status.upper())
    if critic:
        lines.append(f"Critic: {critic}. Review the pack before use.")
    link = _link("/packs")
    if link:
        lines.append(link)
    return await _send("\n".join(lines)[:TELEGRAM_MAX_TEXT])


async def deliver_diagnosis_alert(
    insights: list[dict[str, Any]],
    *,
    generated_at: str,
    llm: dict[str, Any],
) -> dict[str, Any]:
    """Announce new findings, remind about lasting critical ones, report fixes."""
    if not settings.TELEGRAM_ALERTS_ENABLED:
        return {"status": "disabled", "reason": "TELEGRAM_ALERTS_ENABLED=false"}

    now = _now()
    findings = {str(item["dedupe_key"]): item for item in _pushable(insights)}
    raw_state = await redis_client.get(ALERT_STATE_KEY)
    try:
        state = json.loads(raw_state) if raw_state else {}
    except json.JSONDecodeError:
        state = {}
    if not isinstance(state, dict):
        state = {}
    known = _load_active(state)
    cooldown = max(int(settings.TELEGRAM_ALERT_COOLDOWN_SECONDS), 60)

    new: list[dict[str, Any]] = []
    reminders: list[tuple[dict[str, Any], float]] = []
    for key, item in findings.items():
        seen = known.get(key)
        severity = str(item.get("severity"))
        if seen is None or (severity == "critical" and seen.get("severity") != "critical"):
            new.append(item)
            continue
        notified = _parse_datetime(seen.get("notified_at"))
        if severity == "critical" and not _is_event(key) and (
            notified is None or (now - notified).total_seconds() >= cooldown
        ):
            since = _parse_datetime(seen.get("since")) or notified or now
            reminders.append((item, (now - since).total_seconds() / 3600))
    fixed_keys = [key for key in known if key not in findings and not _is_event(key)]
    fixed = [str(known[key].get("title") or "") for key in fixed_keys]
    fixed = [title for title in fixed if title]
    if fixed_keys and not fixed:
        fixed = ["Previously reported problems are no longer observed"]
    severity_rank = {"critical": 0, "warning": 1}
    new.sort(key=lambda item: severity_rank.get(str(item.get("severity")), 2))
    told = {str(item["dedupe_key"]) for item in new} | {str(item["dedupe_key"]) for item, _ in reminders}
    still_open = sum(1 for key in findings if key not in told and not _is_event(key))

    if new or reminders or fixed:
        result = await _send(_diagnosis_message(new, reminders, fixed, still_open))
        delivered = result["status"] == "sent"
    else:
        result = {"status": "suppressed" if findings else "quiet"}
        if findings:
            result["reason"] = "already_announced"
        delivered = True
    if fixed and not findings:
        result["recovery"] = True
    result.update({"finding_count": len(findings), "new": len(new), "reminded": len(reminders), "fixed": len(fixed)})

    if delivered:
        reminded = {str(item["dedupe_key"]) for item, _ in reminders}
        announced = {str(item["dedupe_key"]) for item in new}
        active: dict[str, dict[str, Any]] = {}
        for key, item in findings.items():
            seen = known.get(key) or {}
            touched = key in announced or key in reminded
            active[key] = {
                "severity": item.get("severity"),
                "title": _card(item).title,
                "since": seen.get("since") or now.isoformat(),
                "notified_at": now.isoformat() if touched else seen.get("notified_at"),
            }
        sent_at = now.isoformat() if result["status"] == "sent" else state.get("last_sent_at")
        state = {"active": active, "last_sent_at": sent_at}
    state["last_evaluated_at"] = now.isoformat()
    state["last_delivery"] = result
    ttl = max(settings.SELF_DIAGNOSIS_RESULT_TTL_SECONDS, 60)
    pipe = redis_client.pipeline()
    pipe.set(ALERT_STATE_KEY, json.dumps(state, ensure_ascii=False), ex=ttl)
    pipe.set(
        LAST_DELIVERY_KEY,
        json.dumps({**result, "evaluated_at": now.isoformat(), "generated_at": generated_at}),
        ex=ttl,
    )
    await pipe.execute()
    return result
