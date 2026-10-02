"""Data models for Bundle Mutation Engine, Single-Lever Operators, and Lever Registry (v0.9.0)."""

from enum import Enum
from typing import Any, List
from pydantic import BaseModel


class MutationOperatorType(str, Enum):
    RETRIEVAL_THRESHOLD_MUTATION = "retrieval_threshold_mutation"
    CONTEXT_BUDGET_MUTATION = "context_budget_mutation"
    REPAIR_BUDGET_MUTATION = "repair_budget_mutation"


class MutationSpec(BaseModel):
    operator_type: MutationOperatorType
    target_path: str
    old_value: Any
    new_value: Any
    mutation_distance: float = 0.0
    justification: str


class BundleMutationRecord(BaseModel):
    mutation_id: str
    parent_bundle_id: str
    candidate_hypothesis_id: str
    mutations: List[MutationSpec]
    resulting_bundle_id: str
    created_at_utc: str
