"""Configurable operational budgets — Phase G3.

Budgets fail closed. When a limit is reached, or when current usage cannot be
determined, the action is refused: a resource control that guesses in favour of
proceeding is not a control at all.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Dict, Optional

BUDGET_FILE_NAME = "budgets.json"


class BudgetExceededError(RuntimeError):
    """Raised when an action would exceed a configured budget."""

    def __init__(self, budget: str, limit: int, current: int, requested: int = 1):
        self.budget = budget
        self.limit = limit
        self.current = current
        self.requested = requested
        super().__init__(
            f"Budget '{budget}' exceeded: limit {limit}, current {current}, "
            f"requested {requested}"
        )


class BudgetUnknownError(RuntimeError):
    """Raised when usage for a budget cannot be observed.

    Failing closed means an unreadable state directory blocks the action rather
    than silently granting unlimited capacity.
    """

    def __init__(self, budget: str, detail: str = ""):
        self.budget = budget
        super().__init__(
            f"Budget '{budget}' cannot be enforced: current usage is unknown. {detail}".strip()
        )


@dataclass
class BudgetPolicy:
    """The ten operator-configurable limits mandated by Phase G3."""

    max_active_workspaces: int = 8
    max_workspaces_per_repository: int = 3
    max_experiment_options: int = 8
    max_simultaneous_validations: int = 4
    max_total_retained_artifact_bytes: int = 2 * 1024 * 1024 * 1024
    max_failed_workspace_retention: int = 10
    max_graph_builds: int = 50
    max_portfolio_size: int = 25
    max_process_output_bytes: int = 5 * 1024 * 1024
    max_validation_seconds: int = 900

    def to_dict(self) -> Dict[str, int]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> BudgetPolicy:
        known = {f.name for f in fields(cls)}
        values: Dict[str, int] = {}
        for key, value in (data or {}).items():
            if key not in known:
                continue
            try:
                parsed = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Budget '{key}' must be an integer: {value!r}") from exc
            if parsed < 0:
                raise ValueError(f"Budget '{key}' must not be negative: {parsed}")
            values[key] = parsed
        return cls(**values)

    @classmethod
    def load(cls, path: Path) -> BudgetPolicy:
        """Load a policy, falling back to defaults when none is configured.

        A malformed policy raises rather than defaulting: silently substituting
        generous defaults for an unparsable limit is the failure mode this
        whole phase exists to prevent.
        """
        path = Path(path)
        if not path.is_file():
            return cls()
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def save(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(self.to_dict(), indent=2, sort_keys=True))
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
        return path


@dataclass
class BudgetUsage:
    """One budget's observed usage. `current` is None when unobservable."""

    budget: str
    limit: int
    current: Optional[int]
    unit: str = "count"
    detail: str = ""

    @property
    def exceeded(self) -> bool:
        return self.current is not None and self.current >= self.limit

    @property
    def observable(self) -> bool:
        return self.current is not None

    @property
    def remaining(self) -> Optional[int]:
        return None if self.current is None else max(0, self.limit - self.current)

    @property
    def utilization(self) -> Optional[float]:
        if self.current is None or self.limit <= 0:
            return None
        return round(min(1.0, self.current / self.limit), 4)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "budget": self.budget,
            "limit": self.limit,
            "current": self.current,
            "remaining": self.remaining,
            "utilization": self.utilization,
            "unit": self.unit,
            "exceeded": self.exceeded,
            "observable": self.observable,
            "detail": self.detail,
        }


class BudgetEnforcer:
    """Checks proposed actions against the configured policy."""

    #: Budgets whose unit is not a plain count, for reporting.
    UNITS = {
        "max_total_retained_artifact_bytes": "bytes",
        "max_process_output_bytes": "bytes",
        "max_validation_seconds": "seconds",
    }

    def __init__(self, policy: Optional[BudgetPolicy] = None):
        self.policy = policy or BudgetPolicy()

    def limit_of(self, budget: str) -> int:
        if not hasattr(self.policy, budget):
            raise KeyError(f"Unknown budget: {budget}")
        return int(getattr(self.policy, budget))

    def usage(self, budget: str, current: Optional[int], detail: str = "") -> BudgetUsage:
        return BudgetUsage(
            budget=budget,
            limit=self.limit_of(budget),
            current=None if current is None else int(current),
            unit=self.UNITS.get(budget, "count"),
            detail=detail,
        )

    def check(self, budget: str, current: Optional[int], requested: int = 1) -> BudgetUsage:
        """Refuse the action unless it fits inside the budget.

        Raises `BudgetUnknownError` when usage is unobservable and
        `BudgetExceededError` when the action would cross the limit.
        """
        limit = self.limit_of(budget)
        if current is None:
            raise BudgetUnknownError(budget)
        current = int(current)
        if current + int(requested) > limit:
            raise BudgetExceededError(budget, limit, current, int(requested))
        return self.usage(budget, current)

    def allows(self, budget: str, current: Optional[int], requested: int = 1) -> bool:
        try:
            self.check(budget, current, requested)
        except (BudgetExceededError, BudgetUnknownError):
            return False
        return True

    def report(self, observed: Dict[str, Optional[int]]) -> Dict[str, Any]:
        """Expose every budget with its observed usage — Phase G3/G4."""
        usages = [
            self.usage(field.name, observed.get(field.name)).to_dict()
            for field in fields(self.policy)
        ]
        return {
            "policy": self.policy.to_dict(),
            "usage": usages,
            "exceeded": sorted(u["budget"] for u in usages if u["exceeded"]),
            "unobservable": sorted(u["budget"] for u in usages if not u["observable"]),
            "fail_mode": "closed",
        }


__all__ = [
    "BUDGET_FILE_NAME",
    "BudgetEnforcer",
    "BudgetExceededError",
    "BudgetPolicy",
    "BudgetUnknownError",
    "BudgetUsage",
]
