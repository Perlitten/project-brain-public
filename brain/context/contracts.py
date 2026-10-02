"""Agent response contract helpers built on the single hard-budget primitive."""
from __future__ import annotations

from typing import Any

from brain.context.budget import BudgetedPayloadBuilder, estimated_tokens, utf8_bytes


def transport_metrics(value: str | bytes) -> dict[str, int]:
    raw = value if isinstance(value, bytes) else value.encode("utf-8")
    return {"bytes": len(raw), "estimated_tokens": estimated_tokens(len(raw))}


def bounded_payload(max_bytes: int, metadata: dict[str, Any], **sections: Any) -> dict[str, Any]:
    builder = BudgetedPayloadBuilder(max_bytes, metadata=metadata)
    for name, value in sections.items():
        builder.add(name, value)
    return builder.build()


__all__ = ["BudgetedPayloadBuilder", "bounded_payload", "estimated_tokens", "transport_metrics", "utf8_bytes"]
