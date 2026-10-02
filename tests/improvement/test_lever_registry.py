"""Tests for Bounded Lever Registry and Mutation Rules (v0.9.0)."""

from brain.improvement.mutation.lever_registry import LeverRegistry


def test_lever_registry_bounds_validation():
    registry = LeverRegistry()

    # Valid mutation
    valid, rejections = registry.validate_mutation("routing_thresholds.complexity_threshold", 0.7, 0.85)
    assert valid is True
    assert len(rejections) == 0

    # Min bound violation
    valid, rejections = registry.validate_mutation("routing_thresholds.complexity_threshold", 0.7, 0.05)
    assert valid is False
    assert any("min_value" in r for r in rejections)

    # Max bound violation
    valid, rejections = registry.validate_mutation("routing_thresholds.complexity_threshold", 0.7, 0.99)
    assert valid is False
    assert any("max_value" in r for r in rejections)

    # Max delta violation
    valid, rejections = registry.validate_mutation("routing_thresholds.complexity_threshold", 0.3, 0.8)
    assert valid is False
    assert any("max_delta" in r for r in rejections)


def test_mutation_distance_calculation():
    registry = LeverRegistry()
    dist = registry.compute_mutation_distance("routing_thresholds.complexity_threshold", 0.7, 0.85)
    assert abs(dist - 0.15) < 0.001
