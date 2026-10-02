"""Tests verifying that automated factory identity CANNOT update production champion alias."""

from brain.improvement.promotion import LexicographicPromotionEngine
from brain.improvement.registry.registry import BundleRegistry


def test_factory_identity_cannot_update_champion_alias():
    registry = BundleRegistry()
    engine = LexicographicPromotionEngine(registry)

    # Attempt promote without human approval flag
    res = engine.promote_to_champion("bundle_challenger_xyz", human_approved=False)
    assert res["status"] == "rejected"
    assert "Human approval required" in res["reasons"][0]

    # Confirm alias was NOT mutated
    assert registry.get_alias("champion") != "bundle_challenger_xyz"
