"""Tests for McNemar Exact Test and Holm-Bonferroni Multiplicity Correction (v0.9.0)."""

from brain.improvement.evaluation.statistics import apply_holm_bonferroni_correction, compute_mcnemar_exact_p_value


def test_mcnemar_exact_p_value():
    # 0 discordant -> p = 1.0
    assert compute_mcnemar_exact_p_value(0, 0) == 1.0

    # Symmetric discordant -> p = 1.0
    p_val = compute_mcnemar_exact_p_value(5, 5)
    assert p_val == 1.0

    # High asymmetry -> low p-value
    p_val = compute_mcnemar_exact_p_value(0, 10)
    assert p_val < 0.01


def test_holm_bonferroni_multiplicity_correction():
    raw_p = [0.01, 0.04, 0.03]
    adj_p = apply_holm_bonferroni_correction(raw_p)

    # 0.01 * 3 = 0.03
    assert abs(adj_p[0] - 0.03) < 0.001
    assert adj_p[0] <= adj_p[2] <= adj_p[1]
