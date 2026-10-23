"""Settings saved in the web UI that every process applies, not just the API.

Each process reads ``.env`` and its environment once, at start. The API saves
Settings to its own ``.env``, which the worker container never reads, so the
keys the worker and scheduler run with are also stored in
``brain_runtime_settings`` and applied over the environment: by the API at
start-up, and by the worker and its scheduler every few seconds, so a change
reaches background jobs without a restart. A saved value wins over the
deployment's environment until it is saved again from Settings.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict

from loguru import logger

from brain.config.settings import settings

# Key -> Python type of the live setting. Secrets never go here: the webhook
# secret is only checked by the API, which keeps it in its .env.
SHARED_KEYS: Dict[str, type] = {
    "PROJECT_BRAIN_WEBHOOK_REF": str,
    "BRAIN_WEBHOOK_FETCH_SOURCE": bool,
    "BRAIN_WEBHOOK_FETCH_TIMEOUT_S": int,
    "BRAIN_ORIGIN_POLL_INTERVAL_S": int,
    "BRAIN_ORIGIN_POLL_TIMEOUT_S": int,
    "BRAIN_SOURCE_MIRROR_DIR": str,
}
REFRESH_INTERVAL_S = 15.0
_last_refresh = 0.0


def _valid(key: str, value: Any) -> bool:
    expected = SHARED_KEYS.get(key)
    if expected is None:
        return False
    if key == "BRAIN_SOURCE_MIRROR_DIR" and value is None:
        return True  # cleared: mirrors off
    return type(value) is expected  # bool is an int subclass: exact match only


async def save_shared_settings(values: Dict[str, Any]) -> None:
    """Upsert the shared keys among ``values``. Raises when the database write fails."""
    from sqlalchemy import text

    from brain.database.session import async_session_factory

    rows = [{"key": k, "value": json.dumps(v)} for k, v in values.items() if _valid(k, v)]
    if not rows:
        return
    async with async_session_factory() as session, session.begin():
        await session.execute(text("""
            INSERT INTO brain_runtime_settings (key, value, updated_at) VALUES (:key, :value, CURRENT_TIMESTAMP)
            ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """), rows)


async def load_shared_settings() -> Dict[str, Any]:
    from sqlalchemy import select

    from brain.database.models import BrainRuntimeSetting
    from brain.database.session import async_session_factory

    async with async_session_factory() as session:
        result = await session.execute(
            select(BrainRuntimeSetting.key, BrainRuntimeSetting.value).where(BrainRuntimeSetting.key.in_(SHARED_KEYS))
        )
        rows = result.all()
    out: Dict[str, Any] = {}
    for key, raw in rows:
        try:
            value = json.loads(raw)
        except ValueError:
            continue
        if _valid(key, value):
            out[key] = value
        else:
            logger.warning("Ignoring saved setting {}: unexpected value type", key)
    return out


async def apply_shared_settings() -> Dict[str, Any]:
    """Apply saved values to this process's settings. Never raises: the environment stays in force."""
    global _last_refresh
    _last_refresh = time.monotonic()
    try:
        values = await load_shared_settings()
    except Exception as exc:  # a missing table or a DB outage keeps the current values
        logger.debug("Shared settings unavailable: {}: {}", type(exc).__name__, exc)
        return {}
    for key, value in values.items():
        if getattr(settings, key, None) != value:
            logger.info("Applying {} saved in Settings", key)
            setattr(settings, key, value)
    return values


async def refresh_shared_settings() -> None:
    """:func:`apply_shared_settings`, at most once per :data:`REFRESH_INTERVAL_S`."""
    if time.monotonic() - _last_refresh >= REFRESH_INTERVAL_S:
        await apply_shared_settings()
