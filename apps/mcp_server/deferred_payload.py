"""Deferred delivery of fat MCP tool results.

An MCP server pays for every byte twice: once when the result lands in the
agent's context window, and again in every later turn that re-sends the
transcript. The expensive part of a Brain answer is rarely the code slice the
agent asked for - those are already byte-budgeted in ``brain/context`` - it is
the enumeration wrapped around it: every affected file in an impact projection,
every neighbour in a graph walk, every finding in a diff review. An agent reads
the top of such a list and acts on it; the tail is paid for and ignored.

So the head stays inline and the full result is parked in this process, handed
back as a ``brain://payload/<token>`` URI the agent can read as an MCP resource
on the rare turn it needs the tail. Two consequences worth knowing:

* The store is per-process on purpose. An MCP server is launched by one client
  and lives as long as that session, so there is no cross-client sharing to
  reason about and nothing worth persisting - a token that outlives the process
  is a token whose payload the agent can re-derive by calling the tool again.
* Compaction never drops a scalar. ``status``, ``error`` and ids are what the
  agent branches on, so trimming only ever touches lists and long strings; a
  compacted result must still be a usable answer on its own, because a client
  that ignores resources has to degrade to "smaller", not to "broken".
"""

from __future__ import annotations

import json
import os
from collections import OrderedDict
from time import monotonic
from typing import Any, Callable
from uuid import uuid4

PAYLOAD_URI_SCHEME = "brain"
PAYLOAD_URI_PREFIX = f"{PAYLOAD_URI_SCHEME}://payload/"

# A locator-sized inline budget: enough for a status line plus ~20 paths, which
# is what an agent acts on before it starts reading files for itself.
DEFAULT_INLINE_MAX_BYTES = 2_000
DEFAULT_LIST_HEAD = 20
# Progressively harsher heads, tried in order until the compacted form fits. A
# single pass is not enough: one list of 400 paths and one of 12 need different
# cuts, and guessing a single head for both either over-trims or overflows.
_HEAD_LADDER = (DEFAULT_LIST_HEAD, 5, 1, 0)
_LONG_STRING_BYTES = 400
_MAX_ENTRIES = 32
_TTL_SECONDS = 1800.0


INLINE_BUDGET_ENV = "BRAIN_MCP_INLINE_MAX_BYTES"


def inline_budget() -> int:
    """Inline byte budget for tool results; ``0`` disables deferral entirely.

    An env knob rather than a setting: ``remote_server`` deliberately imports
    nothing from ``brain/``, and an operator who dislikes the trimming needs to
    turn it off where the client launches the server, not by a redeploy.
    """
    raw = os.environ.get(INLINE_BUDGET_ENV, "").strip()
    if not raw:
        return DEFAULT_INLINE_MAX_BYTES
    try:
        parsed = int(raw)
    except ValueError:
        return DEFAULT_INLINE_MAX_BYTES
    return max(0, parsed)


def _dumps(payload: Any) -> str:
    """Serialize the way the transport will, so size decisions match reality."""
    return json.dumps(payload, ensure_ascii=False, default=str)


def _byte_length(text: str) -> int:
    return len(text.encode("utf-8"))


class DeferredPayloadStore:
    """Bounded in-process store of full tool results, keyed by opaque token.

    Bounded twice over - by entry count and by age - because this store sits in
    a long-lived stdio server: an agent that runs a hundred impact analyses in
    one session must not hand the process a hundred retained payloads.
    """

    def __init__(
        self,
        *,
        max_entries: int = _MAX_ENTRIES,
        ttl_seconds: float = _TTL_SECONDS,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._entries: "OrderedDict[str, tuple[float, str]]" = OrderedDict()
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._clock = clock

    def put(self, text: str) -> str:
        """Park already-serialized JSON and return the token that reads it back."""
        self._evict_expired()
        token = uuid4().hex
        self._entries[token] = (self._clock(), text)
        self._entries.move_to_end(token)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)
        return token

    def get(self, token: str) -> str | None:
        self._evict_expired()
        entry = self._entries.get(token)
        if entry is None:
            return None
        self._entries.move_to_end(token)
        return entry[1]

    def uri(self, token: str) -> str:
        return f"{PAYLOAD_URI_PREFIX}{token}"

    def _evict_expired(self) -> None:
        now = self._clock()
        for token, (stored_at, _) in list(self._entries.items()):
            if now - stored_at >= self._ttl_seconds:
                del self._entries[token]


def _trim_value(value: Any, *, head: int) -> tuple[Any, int]:
    """Return ``(trimmed, dropped)`` for one field, leaving scalars untouched."""
    if isinstance(value, list):
        kept = value[:head]
        return kept, len(value) - len(kept)
    if isinstance(value, str) and _byte_length(value) > _LONG_STRING_BYTES:
        return value[:_LONG_STRING_BYTES] + "...", 0
    if isinstance(value, dict):
        trimmed: dict[str, Any] = {}
        dropped = 0
        for key, nested in value.items():
            trimmed[key], nested_dropped = _trim_value(nested, head=head)
            dropped += nested_dropped
        return trimmed, dropped
    return value, 0


def compact_payload(
    payload: Any,
    store: DeferredPayloadStore,
    *,
    inline_max_bytes: int | None = None,
) -> Any:
    """Keep ``payload`` inline when it is small; otherwise trim it and park the rest.

    Only ``dict`` results are compacted. A tool that returns a bare string owns
    its own budget upstream (``ask_project`` is capped by the API), and cutting a
    prose answer in half here would damage the answer rather than the transcript.
    """
    if inline_max_bytes is None:
        inline_max_bytes = inline_budget()
    if not isinstance(payload, dict) or inline_max_bytes <= 0:
        return payload

    full_text = _dumps(payload)
    if _byte_length(full_text) <= inline_max_bytes:
        return payload

    token = store.put(full_text)
    for head in _HEAD_LADDER:
        compacted: dict[str, Any] = {}
        dropped = 0
        for key, value in payload.items():
            compacted[key], value_dropped = _trim_value(value, head=head)
            dropped += value_dropped
        compacted["_full"] = {
            "uri": store.uri(token),
            "bytes": _byte_length(full_text),
            "omitted_items": dropped,
            "note": "Trimmed to save context. Read this MCP resource for the complete result.",
        }
        if _byte_length(_dumps(compacted)) <= inline_max_bytes or head == _HEAD_LADDER[-1]:
            return compacted
    return payload  # pragma: no cover - the ladder always returns on its last rung


def register_payload_resource(mcp: Any, store: DeferredPayloadStore) -> None:
    """Expose ``store`` as the MCP resource that ``compact_payload`` points at."""

    @mcp.resource(PAYLOAD_URI_PREFIX + "{token}")
    def deferred_payload(token: str) -> str:
        """Full result of an earlier tool call, parked outside the context window."""
        text = store.get(token)
        if text is None:
            # An expired token is not an error worth raising: the agent can get
            # the same payload by calling the tool again, and saying so beats a
            # protocol-level failure it has to interpret.
            return _dumps(
                {
                    "error": "This deferred payload is no longer held; call the tool again to rebuild it.",
                    "token": token,
                }
            )
        return text
