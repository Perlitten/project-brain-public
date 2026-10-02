"""Single-Lever Bundle Mutator with Lever Registry Validation for Project Brain v0.9.0."""

import hashlib
import json
import time
from typing import List, Optional, Tuple
from brain.improvement.models import CandidateHypothesis
from brain.improvement.mutation.lever_registry import LeverRegistry
from brain.improvement.mutation.models import BundleMutationRecord, MutationOperatorType, MutationSpec
from brain.improvement.registry.bundle import AgentBundleManifest


class SingleLeverBundleMutator:
    """Applies validated single-lever mutations to champion AgentBundle manifests."""

    def __init__(self, lever_registry: Optional[LeverRegistry] = None):
        self.lever_registry = lever_registry or LeverRegistry()

    def mutate_bundle(
        self,
        parent_manifest: AgentBundleManifest,
        hypothesis: CandidateHypothesis,
    ) -> Tuple[AgentBundleManifest, BundleMutationRecord]:
        """Synthesizes a new AgentBundleManifest by applying bounded single-lever changes."""
        mutated_dict = parent_manifest.model_dump()
        mutation_specs: List[MutationSpec] = []

        for lever in hypothesis.lever_changes:
            lever_name = lever.get("lever")
            lever.get("parameter", "")
            val = lever.get("new_value")
            if not lever_name or val is None:
                continue


            if lever_name in mutated_dict.get("context_budgets", {}):
                old_val = mutated_dict["context_budgets"][lever_name]
                valid, rejections = self.lever_registry.validate_mutation(f"context_budgets.{lever_name}", old_val, val)
                if not valid:
                    continue
                mutated_dict["context_budgets"][lever_name] = int(val)
                dist = self.lever_registry.compute_mutation_distance(f"context_budgets.{lever_name}", old_val, val)
                mutation_specs.append(
                    MutationSpec(
                        operator_type=MutationOperatorType.CONTEXT_BUDGET_MUTATION,
                        target_path=f"context_budgets.{lever_name}",
                        old_value=old_val,
                        new_value=val,
                        mutation_distance=dist,
                        justification=hypothesis.hypothesis,
                    )
                )
            elif lever_name in mutated_dict.get("routing_thresholds", {}) or lever_name == "routing_thresholds":
                routing_dict = mutated_dict.setdefault("routing_thresholds", {})
                old_val = routing_dict.get("complexity_threshold", 0.7)
                valid, rejections = self.lever_registry.validate_mutation("routing_thresholds.complexity_threshold", old_val, val)
                if not valid:
                    continue
                routing_dict["complexity_threshold"] = float(val)
                dist = self.lever_registry.compute_mutation_distance("routing_thresholds.complexity_threshold", old_val, val)
                mutation_specs.append(
                    MutationSpec(
                        operator_type=MutationOperatorType.RETRIEVAL_THRESHOLD_MUTATION,
                        target_path="routing_thresholds.complexity_threshold",
                        old_value=old_val,
                        new_value=val,
                        mutation_distance=dist,
                        justification=hypothesis.hypothesis,
                    )
                )
            elif lever_name in mutated_dict.get("repair_budgets", {}):
                old_val = mutated_dict["repair_budgets"][lever_name]
                valid, rejections = self.lever_registry.validate_mutation(f"repair_budgets.{lever_name}", old_val, val)
                if not valid:
                    continue
                mutated_dict["repair_budgets"][lever_name] = int(val)
                dist = self.lever_registry.compute_mutation_distance(f"repair_budgets.{lever_name}", old_val, val)
                mutation_specs.append(
                    MutationSpec(
                        operator_type=MutationOperatorType.REPAIR_BUDGET_MUTATION,
                        target_path=f"repair_budgets.{lever_name}",
                        old_value=old_val,
                        new_value=val,
                        mutation_distance=dist,
                        justification=hypothesis.hypothesis,
                    )
                )

        # Generate new content-addressed bundle ID
        manifest_serialized = json.dumps(mutated_dict, sort_keys=True)
        content_hash = hashlib.sha256(manifest_serialized.encode("utf-8")).hexdigest()[:12]
        new_bundle_id = f"bundle_{hypothesis.candidate_id}_{content_hash}"
        mutated_dict["bundle_id"] = new_bundle_id

        new_manifest = AgentBundleManifest(**mutated_dict)

        mutation_record = BundleMutationRecord(
            mutation_id=f"mut-{content_hash}",
            parent_bundle_id=parent_manifest.bundle_id,
            candidate_hypothesis_id=hypothesis.candidate_id,
            mutations=mutation_specs,
            resulting_bundle_id=new_bundle_id,
            created_at_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )

        return new_manifest, mutation_record
