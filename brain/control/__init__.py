"""Operator control plane — Workstream G.

A stable API and CLI over the v0.5.0 subsystems: what is registered, what is
fresh, what is running, what failed, what is waiting for a human decision, and
how much of each configured budget is in use. Budgets fail closed.

Project Brain may apply candidate patches only inside disposable managed
workspaces for validation. It does not apply patches to authoritative
repositories, commit changes, push branches, merge pull requests, or deploy
software.
"""

from brain.control.budgets import (
    BUDGET_FILE_NAME,
    BudgetEnforcer,
    BudgetExceededError,
    BudgetPolicy,
    BudgetUnknownError,
    BudgetUsage,
)
from brain.control.plane import (
    ALLOWED_ACTIONS,
    FORBIDDEN_ACTION_VERBS,
    PRIORITY_LABELS,
    ActionItem,
    ControlPlane,
    parse_timestamp,
)

__all__ = [
    "ALLOWED_ACTIONS",
    "BUDGET_FILE_NAME",
    "FORBIDDEN_ACTION_VERBS",
    "PRIORITY_LABELS",
    "ActionItem",
    "BudgetEnforcer",
    "BudgetExceededError",
    "BudgetPolicy",
    "BudgetUnknownError",
    "BudgetUsage",
    "ControlPlane",
    "parse_timestamp",
]
