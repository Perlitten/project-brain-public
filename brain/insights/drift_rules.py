"""Declarative Drift Rule Registry for Project Brain Architectural Drift Intelligence v2 (Phase 2)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass(frozen=True)
class DriftRule:
    """Declarative definition of an architectural layer boundary constraint."""

    rule_id: str
    name: str
    description: str
    severity: str
    source_layer_pattern: str  # e.g., "apps/api/" or "brain/workers/"
    forbidden_import_pattern: str  # e.g., "sqlalchemy.orm.session"
    remediation_guidance: str
    allowed_exceptions: List[str] = field(default_factory=list)
    enabled: bool = True
    version: str = "1.0.0"


BUILTIN_DRIFT_RULES: List[DriftRule] = [
    DriftRule(
        rule_id="DRIFT-001",
        name="api_raw_orm_bypass",
        description="API routes must access database via repository_utils or service layer rather than raw ORM session internals.",
        severity="warning",
        source_layer_pattern="apps/api/",
        forbidden_import_pattern="sqlalchemy.orm.session",
        remediation_guidance="Replace direct sqlalchemy.orm.session imports with brain.database.repository_utils or async_session_factory wrapper.",
    ),
    DriftRule(
        rule_id="DRIFT-002",
        name="worker_ui_coupling",
        description="Background worker components must remain decoupled from UI asset renderers.",
        severity="critical",
        source_layer_pattern="brain/workers/",
        forbidden_import_pattern="apps.api.static",
        remediation_guidance="Refactor worker logic to operate on structured payloads without referencing apps.api.static.",
    ),
    DriftRule(
        rule_id="DRIFT-003",
        name="core_retrieval_router_dependency",
        description="Core retrieval pipeline modules must not depend directly on HTTP API routers.",
        severity="critical",
        source_layer_pattern="brain/retrieval/",
        forbidden_import_pattern="apps.api.routers",
        remediation_guidance="Decouple retrieval algorithms from HTTP API router layers.",
    ),
    DriftRule(
        rule_id="DRIFT-005",
        name="core_domain_deployment_script_coupling",
        description="Core domain logic must not import deployment shell wrappers directly.",
        severity="warning",
        source_layer_pattern="brain/memory/",
        forbidden_import_pattern="deploy",
        remediation_guidance="Isolate deployment configuration from runtime memory modules.",
    ),
]


class DriftRuleRegistry:
    """Registry managing active architectural rules."""

    def __init__(self, rules: Optional[List[DriftRule]] = None):
        self._rules: dict[str, DriftRule] = {}
        for r in rules or BUILTIN_DRIFT_RULES:
            self.register_rule(r)

    def register_rule(self, rule: DriftRule) -> None:
        self._rules[rule.rule_id] = rule

    def get_rule(self, rule_id: str) -> Optional[DriftRule]:
        return self._rules.get(rule_id)

    def list_active_rules(self) -> List[DriftRule]:
        return [r for r in self._rules.values() if r.enabled]


_DEFAULT_REGISTRY = DriftRuleRegistry()


def get_default_rule_registry() -> DriftRuleRegistry:
    return _DEFAULT_REGISTRY
