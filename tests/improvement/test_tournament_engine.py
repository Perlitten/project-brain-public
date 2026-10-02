"""Tests for Multi-Agent Champion-Anchored Tournament League & Sealed Finalist Confirmation Engine (Milestone v0.9.0)."""

from brain.improvement.hypothesis_generator import SingleLeverHypothesisGenerator
from brain.improvement.mutation.mutator import SingleLeverBundleMutator
from brain.improvement.registry.bundle import create_agent_bundle
from brain.improvement.tournament.engine import MultiAgentTournamentEngine
from brain.improvement.tournament.models import TournamentStatus


def test_bundle_mutator_single_lever():
    parent_manifest = create_agent_bundle("bundle_test_parent")
    generator = SingleLeverHypothesisGenerator()
    hypothesis = generator.generate_hypothesis(
        cluster_id="cluster-premature-edit",
        failure_trajectories=[],
        target_lever="routing_thresholds",
    )

    mutator = SingleLeverBundleMutator()
    mutated_manifest, mutation_rec = mutator.mutate_bundle(parent_manifest, hypothesis)

    assert mutated_manifest.bundle_id != parent_manifest.bundle_id
    assert mutation_rec.parent_bundle_id == "bundle_test_parent"
    assert mutation_rec.candidate_hypothesis_id == hypothesis.candidate_id
    assert len(mutation_rec.mutations) == 1
    assert mutation_rec.mutations[0].target_path == "routing_thresholds.complexity_threshold"
    assert mutation_rec.mutations[0].mutation_distance > 0.0


def test_multi_agent_tournament_engine():
    engine = MultiAgentTournamentEngine()
    result = engine.run_tournament(
        target_cluster_ids=["cluster-premature-edit", "cluster-context-overflow"],
        champion_bundle_id="bundle_champion_v070",
    )

    assert result.tournament_id.startswith("tourn-")
    assert result.champion_bundle_id == "bundle_champion_v070"
    assert len(result.candidates) == 3  # Champion + 2 challengers
    assert len(result.league_comparisons) == 2
    assert result.finalist_selection.sealed_t3_evaluated is True
    assert result.finalist_selection.sealed_t3_passed is True
    assert result.factory_attestation_hash.startswith("attest-")
    assert result.status == TournamentStatus.WINNER_PROMOTED
