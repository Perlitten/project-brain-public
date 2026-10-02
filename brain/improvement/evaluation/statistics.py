"""Statistical Analysis Engine: Exact McNemar Test, Paired Bootstrap, and Holm Multiplicity Correction."""

import math
from typing import List, Optional, Tuple


def compute_mcnemar_exact_p_value(
    b: Optional[int] = None,
    c: Optional[int] = None,
    win_champ_loss_challenger: Optional[int] = None,
    loss_champ_win_challenger: Optional[int] = None,
) -> float:
    """Computes exact two-tailed McNemar p-value for paired binary outcomes."""
    if b is None and win_champ_loss_challenger is not None:
        b = win_champ_loss_challenger
    if c is None and loss_champ_win_challenger is not None:
        c = loss_champ_win_challenger

    b_val = b or 0
    c_val = c or 0
    n = b_val + c_val
    if n == 0:
        return 1.0

    k = min(b_val, c_val)
    cumulative_prob = 0.0
    for i in range(0, k + 1):
        cumulative_prob += math.comb(n, i) * (0.5 ** n)

    p_val = min(1.0, 2.0 * cumulative_prob)
    return float(p_val)


def apply_holm_bonferroni_correction(p_values: List[float]) -> List[float]:
    """Applies Holm-Bonferroni multiplicity correction to a set of p-values."""
    m = len(p_values)
    if m == 0:
        return []

    indexed_p = sorted(enumerate(p_values), key=lambda x: x[1])
    adjusted = [0.0] * m
    running_max = 0.0

    for rank, (original_idx, p_val) in enumerate(indexed_p):
        multiplier = m - rank
        adj = min(1.0, p_val * multiplier)
        running_max = max(running_max, adj)
        adjusted[original_idx] = float(running_max)

    return adjusted


def compute_paired_bootstrap_ci(
    champ_scores: List[float],
    challenger_scores: List[float],
    iterations: int = 1000,
    confidence_level: float = 0.95,
) -> Tuple[float, float]:
    """Computes non-parametric paired bootstrap confidence interval for score delta."""
    if not champ_scores or len(champ_scores) != len(challenger_scores):
        return (-0.05, 0.05)

    deltas = [c - ch for ch, c in zip(challenger_scores, champ_scores)]
    n = len(deltas)
    if n == 0:
        return (-0.05, 0.05)

    import random

    sample_means = []
    for _ in range(iterations):
        sample = [random.choice(deltas) for _ in range(n)]
        sample_means.append(sum(sample) / float(n))

    sample_means.sort()
    lower_idx = int((1.0 - confidence_level) / 2.0 * iterations)
    upper_idx = int((1.0 - (1.0 - confidence_level) / 2.0) * iterations)

    return (sample_means[lower_idx], sample_means[upper_idx])
