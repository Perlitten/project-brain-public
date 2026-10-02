"""Proactive insight generation and storage."""

from brain.insights.proactive import (
    INSIGHT_ENGINE_VERSION,
    generate_proactive_insights,
    list_recent_insights,
)

__all__ = [
    "INSIGHT_ENGINE_VERSION",
    "generate_proactive_insights",
    "list_recent_insights",
]
