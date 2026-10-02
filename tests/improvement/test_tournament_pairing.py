"""Tests for Champion-Anchored League Pairing and Lineage Tracking (v0.9.0)."""

from brain.improvement.tournament.engine import MultiAgentTournamentEngine


def test_champion_anchored_pairing_and_lineage():
    engine = MultiAgentTournamentEngine()
    result = engine.run_tournament(
        target_cluster_ids=["cluster-premature-edit", "cluster-context-overflow"],
        champion_bundle_id="bundle_champion_v070",
    )

    assert result.champion_bundle_id == "bundle_champion_v070"
    assert len(result.candidates) == 3  # Champion + 2 Challengers

    champion_entry = [c for c in result.candidates if c.is_champion][0]
    assert champion_entry.bundle_id == "bundle_champion_v070"

    challengers = [c for c in result.candidates if not c.is_champion]
    for ch in challengers:
        assert ch.parent_bundle_id == "bundle_champion_v070"
        assert ch.bundle_digest is not None
        assert ch.generation == 1

    for comp in result.league_comparisons:
        assert comp.champion_bundle_id == "bundle_champion_v070"
        assert comp.evaluation_pool_type == "development_replay"
