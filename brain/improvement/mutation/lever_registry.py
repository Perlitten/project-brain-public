"""Typed Lever Registry and Bounded Mutation Rules for Project Brain v0.9.0."""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class LeverDefinition:
    """Formal schema and invariant bounds for a causal control dimension."""

    lever_id: str
    family: str  # e.g., "retrieval", "context", "repair"
    target_path: str
    value_type: type  # float, int, dict
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    max_delta_per_generation: Optional[float] = None
    requires_rebalance: bool = False
    incompatible_levers: List[str] = field(default_factory=list)
    risk_class: str = "LOW"  # LOW, MEDIUM, HIGH


class LeverRegistry:
    """Authoritative registry of pre-registered causal control levers and mutation bounds."""

    def __init__(self):
        self._levers: Dict[str, LeverDefinition] = {}
        self._register_default_levers()

    def _register_default_levers(self):
        """Registers canonical single-lever control dimensions."""
        self.register(
            LeverDefinition(
                lever_id="routing_thresholds.complexity_threshold",
                family="routing",
                target_path="routing_thresholds.complexity_threshold",
                value_type=float,
                min_value=0.1,
                max_value=0.95,
                max_delta_per_generation=0.25,
                risk_class="LOW",
            )
        )
        self.register(
            LeverDefinition(
                lever_id="context_budgets.max_symbol_bytes",
                family="context",
                target_path="context_budgets.max_symbol_bytes",
                value_type=int,
                min_value=1000,
                max_value=100000,
                max_delta_per_generation=20000,
                risk_class="LOW",
            )
        )
        self.register(
            LeverDefinition(
                lever_id="repair_budgets.max_repair_attempts",
                family="repair",
                target_path="repair_budgets.max_repair_attempts",
                value_type=int,
                min_value=1,
                max_value=10,
                max_delta_per_generation=3,
                risk_class="MEDIUM",
            )
        )

    def register(self, definition: LeverDefinition):
        self._levers[definition.lever_id] = definition

    def get(self, lever_id: str) -> Optional[LeverDefinition]:
        return self._levers.get(lever_id)

    def validate_mutation(
        self,
        lever_id: str,
        old_val: Any,
        new_val: Any,
    ) -> Tuple[bool, List[str]]:
        """Empirically validates mutation bounds, types, and max delta limits."""
        lever = self.get(lever_id)
        if not lever:
            return False, [f"Lever '{lever_id}' is not registered in LeverRegistry."]

        rejections = []
        try:
            typed_val = lever.value_type(new_val)
        except (ValueError, TypeError):
            return False, [f"Value '{new_val}' cannot be cast to type {lever.value_type.__name__} for lever '{lever_id}'."]

        if lever.min_value is not None and float(typed_val) < lever.min_value:
            rejections.append(f"Value {typed_val} violates min_value bound {lever.min_value} for lever '{lever_id}'.")

        if lever.max_value is not None and float(typed_val) > lever.max_value:
            rejections.append(f"Value {typed_val} violates max_value bound {lever.max_value} for lever '{lever_id}'.")

        if lever.max_delta_per_generation is not None and old_val is not None:
            delta = abs(float(typed_val) - float(old_val))
            if delta > lever.max_delta_per_generation:
                rejections.append(f"Delta {delta} exceeds max_delta_per_generation limit {lever.max_delta_per_generation} for lever '{lever_id}'.")

        return len(rejections) == 0, rejections

    def compute_mutation_distance(self, lever_id: str, old_val: Any, new_val: Any) -> float:
        """Calculates normalized distance metric for mutation magnitude tracking."""
        try:
            return abs(float(new_val) - float(old_val))
        except (ValueError, TypeError):
            return 1.0
