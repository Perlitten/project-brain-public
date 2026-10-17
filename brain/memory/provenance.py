"""Provenance and versioning primitives shared by the memory stores.

Every memory object (decision, rule, learning, episode, skill) names the principal
that created it (``author_principal_id``) and the one behind its latest change
(``updated_by_principal_id``). Decisions and rules additionally keep a supersede
chain with a monotonic ``version`` that doubles as the compare-and-swap token.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional, Set, Union

MemoryKey = Union[int, str]


class MemoryVersionConflict(Exception):
    """``expected_version`` did not match the stored row: someone else changed it first."""

    def __init__(self, kind: str, key: MemoryKey, expected_version: int, actual_version: int):
        self.kind = kind
        self.key = key
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            f"{kind} {key!r} is at version {actual_version}, not {expected_version}; "
            "re-read it and retry with the current version"
        )


class SelfApprovalError(PermissionError):
    """The principal that produced the evidence may not also confirm it (team state)."""


@dataclass(frozen=True)
class MemoryWriteResult:
    """What a decision/rule write did: the canonical id, its version and the history row, if any."""

    id: MemoryKey
    version: int
    created: bool
    changed: bool
    superseded_id: Optional[MemoryKey] = None

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"version": self.version, "created": self.created, "changed": self.changed}
        if self.superseded_id is not None:
            data["superseded_id"] = self.superseded_id
        return data


def check_expected_version(kind: str, key: MemoryKey, expected_version: Optional[int],
                           actual_version: Optional[int]) -> None:
    """Compare-and-swap guard. ``actual_version`` None means no canonical row (version 0)."""
    if expected_version is None:
        return
    actual = int(actual_version or 0)
    if int(expected_version) != actual:
        raise MemoryVersionConflict(kind, key, int(expected_version), actual)


def distinct_principals(evidence: Optional[Iterable[Any]]) -> Set[int]:
    """Distinct ``author_principal_id`` values across evidence refs (unknown authors are skipped)."""
    found: Set[int] = set()
    for item in evidence or ():
        if not isinstance(item, dict):
            continue
        principal_id = item.get("author_principal_id")
        if principal_id is None:
            continue
        try:
            found.add(int(principal_id))
        except (TypeError, ValueError):
            continue
    return found


def sole_principal(evidence: Optional[Iterable[Any]]) -> Optional[int]:
    """The one principal behind the evidence, or None when it is mixed or unknown."""
    principals = distinct_principals(evidence)
    if len(principals) == 1:
        return next(iter(principals))
    return None
