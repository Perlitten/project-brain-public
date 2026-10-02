"""Tests verifying SEALED_HOLDOUT_IS_NOT_A_TOURNAMENT_POOL invariant and single T3 confirmation pass."""

from brain.improvement.tournament.engine import MultiAgentTournamentEngine


def test_multiple_challengers_can_never_access_sealed_pool_in_league():
    """Verifies that development league comparisons use ONLY development_replay pool."""
    engine = MultiAgentTournamentEngine()
    result = engine.run_tournament(
        target_cluster_ids=["cluster-premature-edit", "cluster-context-overflow"],
        champion_bundle_id="bundle_champion_v070",
    )

    # All league comparisons must be development_replay
    for comp in result.league_comparisons:
        assert comp.evaluation_pool_type == "development_replay"

    # Only ONE finalist gets to evaluate on sealed_promotion_holdout
    if result.sealed_t3_report:
        assert result.sealed_t3_report.evaluation_pool_type == "sealed_promotion_holdout"
        assert result.sealed_t3_report.challenger_bundle_id == result.finalist_bundle_id
