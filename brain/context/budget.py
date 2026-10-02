"""Hard UTF-8 transport budgets for every agent-facing payload.

Provider tokenizers differ and can change independently of the API.  A byte cap
is therefore the invariant; ``ceil(bytes / 4)`` is the stable reporting metric.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Iterable


def utf8_bytes(value: str) -> int:
    return len(value.encode("utf-8"))


def estimated_tokens(byte_count: int) -> int:
    return math.ceil(max(0, byte_count) / 4)


def truncate_utf8(value: str, max_bytes: int, suffix: str = "…") -> str:
    """Return a deterministic, valid UTF-8 prefix that never exceeds ``max_bytes``."""
    if max_bytes <= 0:
        return ""
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    suffix_bytes = suffix.encode("utf-8")
    if len(suffix_bytes) > max_bytes:
        return encoded[:max_bytes].decode("utf-8", errors="ignore")
    prefix = encoded[: max_bytes - len(suffix_bytes)].decode("utf-8", errors="ignore")
    return prefix + suffix


class BudgetExceeded(ValueError):
    """The required envelope cannot fit the declared transport cap."""


@dataclass
class ContextBudget:
    """Mutable accounting for one response or prompt.

    Sections are added in a stable caller-defined order.  Once a section runs
    out of its allocation or the global cap, it is truncated and the reason is
    retained in the compact response metadata instead of leaking raw debug data.
    """

    max_bytes: int
    allocations: dict[str, int] = field(default_factory=dict)
    consumed_bytes: int = 0
    sections: dict[str, int] = field(default_factory=dict)
    exclusions: list[dict[str, str]] = field(default_factory=list)
    truncated: bool = False

    def __post_init__(self) -> None:
        if self.max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if any(value < 0 for value in self.allocations.values()):
            raise ValueError("section allocations cannot be negative")

    @property
    def remaining_bytes(self) -> int:
        return max(0, self.max_bytes - self.consumed_bytes)

    def exclude(self, section: str, reason: str) -> None:
        self.truncated = True
        if len(self.exclusions) < 24:
            self.exclusions.append({"section": str(section)[:80], "reason": str(reason)[:120]})

    def add_text(self, section: str, value: str, *, allocation: int | None = None) -> str:
        requested = utf8_bytes(value)
        section_cap = self.allocations.get(section, self.max_bytes)
        if allocation is not None:
            section_cap = min(section_cap, allocation)
        section_cap = min(section_cap, self.remaining_bytes)
        rendered = truncate_utf8(value, section_cap)
        rendered_bytes = utf8_bytes(rendered)
        self.consumed_bytes += rendered_bytes
        self.sections[section] = self.sections.get(section, 0) + rendered_bytes
        if rendered_bytes < requested:
            self.exclude(section, "byte_cap")
        return rendered

    def snapshot(self) -> dict[str, Any]:
        return {
            "estimated_tokens": estimated_tokens(self.consumed_bytes),
            "bytes": self.consumed_bytes,
            "limit": self.max_bytes,
            "truncated": self.truncated,
            "sections": dict(sorted(self.sections.items())),
            "excluded": list(self.exclusions),
        }


def _payload_bytes(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


class BudgetedPayloadBuilder:
    """Build a JSON-safe envelope whose complete UTF-8 representation is bounded.

    ``add`` works for strings and ordered lists. Dict/scalar values are all or
    nothing: silently slicing a structural value is unsafe.  The builder is
    intentionally deterministic, including exclusion order, to make shadow
    comparisons and property tests reproducible.
    """

    def __init__(self, max_bytes: int, *, metadata: dict[str, Any] | None = None) -> None:
        self.budget = ContextBudget(max_bytes=max_bytes)
        self._metadata = dict(metadata or {})
        self._fields: list[tuple[str, Any, int]] = []

    def add(self, section: str, value: Any, *, priority: int = 0) -> "BudgetedPayloadBuilder":
        self._fields.append((section, value, priority))
        return self

    def _envelope(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = dict(payload)
        result["budget"] = self.budget.snapshot()
        return result

    def _fits(self, payload: dict[str, Any]) -> bool:
        return _payload_bytes(self._envelope(payload)) <= self.budget.max_bytes

    def _fit_string(self, payload: dict[str, Any], section: str, value: str) -> str:
        remaining = self.budget.max_bytes - _payload_bytes(self._envelope(payload))
        # JSON escaping and the field name add overhead. Decrease one byte at a
        # time only for the short tail; the first estimate avoids quadratic work.
        candidate = truncate_utf8(value, max(0, remaining - 32))
        while candidate and not self._fits({**payload, section: candidate}):
            candidate = truncate_utf8(candidate, max(0, utf8_bytes(candidate) - 1))
        return candidate

    def build(self) -> dict[str, Any]:
        payload = dict(self._metadata)
        added_sections: list[str] = []
        if not self._fits(payload):
            raise BudgetExceeded("Response metadata exceeds hard byte cap")
        # Higher priority is admitted first; insertion order breaks ties.
        ordered_fields = sorted(
            enumerate(self._fields), key=lambda item: (-item[1][2], item[0])
        )
        for _index, (section, value, _priority) in ordered_fields:
            if isinstance(value, str):
                fitted = self._fit_string(payload, section, value)
                if not fitted and value:
                    self.budget.exclude(section, "envelope_overhead")
                    continue
                payload[section] = fitted
                added_sections.append(section)
                self.budget.add_text(section, fitted)
                if fitted != value:
                    self.budget.exclude(section, "byte_cap")
                continue
            if isinstance(value, list):
                accepted: list[Any] = []
                for item in value:
                    candidate = {**payload, section: [*accepted, item]}
                    if self._fits(candidate):
                        accepted.append(item)
                        # Update consumed_bytes so the envelope accurately
                        # reflects the growing payload size during list
                        # processing. Without this, the _fits check uses a
                        # stale envelope that underestimates the total.
                        self.budget.sections[section] = _payload_bytes({section: [*accepted]})
                        self.budget.consumed_bytes = sum(self.budget.sections.values())
                    else:
                        self.budget.exclude(section, "byte_cap")
                        break
                if accepted:
                    payload[section] = accepted
                    added_sections.append(section)
                    self.budget.sections[section] = _payload_bytes({section: accepted})
                elif value:
                    self.budget.exclude(section, "envelope_overhead")
                continue
            if self._fits({**payload, section: value}):
                payload[section] = value
                added_sections.append(section)
                self.budget.sections[section] = _payload_bytes({section: value})
            else:
                self.budget.exclude(section, "byte_cap")
        result = self._envelope(payload)
        # Exclusion metadata itself can grow after the final field. Compact it
        # deterministically until the *actual* serialized payload fits.
        while _payload_bytes(result) > self.budget.max_bytes and added_sections:
            removed = added_sections.pop()
            payload.pop(removed, None)
            self.budget.sections.pop(removed, None)
            self.budget.truncated = True
            result = self._envelope(payload)
        while _payload_bytes(result) > self.budget.max_bytes and self.budget.exclusions:
            self.budget.exclusions.pop()
            result = self._envelope(payload)
        if _payload_bytes(result) > self.budget.max_bytes:
            raise BudgetExceeded("Response cannot fit hard byte cap")
        # Report the complete serialized transport envelope, not only the
        # admitted sections. Updating the value can change JSON digit counts,
        # so settle this small fixed point before returning it.
        for _ in range(3):
            actual_bytes = _payload_bytes(result)
            if self.budget.consumed_bytes == actual_bytes:
                break
            self.budget.consumed_bytes = actual_bytes
            result = self._envelope(payload)
        if _payload_bytes(result) > self.budget.max_bytes:
            raise BudgetExceeded("Budget metadata exceeds hard byte cap")
        return result


def bounded_join(budget: ContextBudget, section: str, values: Iterable[str], separator: str = "\n") -> str:
    """Append values until the section/global allocation is exhausted."""
    accepted: list[str] = []
    for value in values:
        candidate = separator.join([*accepted, value])
        if utf8_bytes(candidate) > min(budget.allocations.get(section, budget.max_bytes), budget.remaining_bytes):
            budget.exclude(section, "byte_cap")
            break
        accepted.append(value)
    return budget.add_text(section, separator.join(accepted))
