"""Telegram Notification Guard for GoalRun Service (v0.9.0).

Enforces at most one ACCEPTED and one terminal notification (COMPLETED, BLOCKED, ROLLED_BACK)
per goal_id, preventing alert spam while preserving operational visibility.
"""
from __future__ import annotations

import logging
from typing import Set

logger = logging.getLogger(__name__)

_NOTIFIED_ACCEPTED: Set[str] = set()
_NOTIFIED_TERMINAL: Set[str] = set()


def emit_goal_notification(goal_id: str, phase: str, details: str = "") -> bool:
    """Emit deduplicated Telegram notification bounded to 1 ACCEPTED and 1 terminal per goal."""
    phase_upper = str(phase).upper()

    if phase_upper in ("QUEUED", "ACCEPTED", "GOAL_RECEIVED", "INTERNAL_PLANNING"):
        if goal_id in _NOTIFIED_ACCEPTED:
            return False
        _NOTIFIED_ACCEPTED.add(goal_id)
        logger.info("[Telegram] Goal %s ACCEPTED: %s", goal_id, details)
        return True

    if phase_upper in ("CLOSED", "COMPLETED", "BLOCKED", "BLOCKED_ESCALATED", "ROLLED_BACK", "CANCELLED"):
        if goal_id in _NOTIFIED_TERMINAL:
            return False
        _NOTIFIED_TERMINAL.add(goal_id)
        logger.info("[Telegram] Goal %s Terminal %s: %s", goal_id, phase_upper, details)
        return True

    return False
