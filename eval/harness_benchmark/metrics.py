"""Metric functions for the harness effectiveness benchmark.

All metrics compare the harness output against a human-curated ground truth
declared in each task file. Higher is better for every metric.
"""


def hit_rate(expected, got):
    """Fraction of expected items present in the got list (0..1)."""
    expected = list(expected)
    if not expected:
        return 1.0
    got_set = set(got)
    return sum(1 for e in expected if e in got_set) / len(expected)


def precision_at_k(expected, got, k):
    """Fraction of the top-k results that are expected (0..1)."""
    top = list(got)[:k]
    if not top:
        return 0.0
    expected_set = set(expected)
    return sum(1 for g in top if g in expected_set) / len(top)


def recall_at_k(expected, got, k):
    """Fraction of expected items found within the top-k results (0..1)."""
    return hit_rate(expected, list(got)[:k])


def noise_ratio(expected, nice, got):
    """Fraction of results that are neither expected nor nice-to-have (0..1).

    Measures how much irrelevant material the harness returns alongside
    the useful files.
    """
    got = list(got)
    if not got:
        return 0.0
    allowed = set(expected) | set(nice)
    return sum(1 for g in got if g not in allowed) / len(got)


def detection(must_detect_keywords, review_text):
    """True when every keyword appears (case-insensitive) in the review text."""
    text = (review_text or "").lower()
    return all(kw.lower() in text for kw in must_detect_keywords)


def mrr(expected, got):
    """Mean Reciprocal Rank: 1/rank of the first expected item found (0..1).

    Rewards putting the right files early, not just somewhere in the list.
    """
    expected_set = set(expected)
    if not expected_set:
        return 1.0
    for i, g in enumerate(got):
        if g in expected_set:
            return 1.0 / (i + 1)
    return 0.0


def percentile(values, p):
    """p-th percentile of values (0..100)."""
    values = sorted(values)
    if not values:
        return 0.0
    k = (len(values) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(values) - 1)
    if f == c:
        return values[f]
    return values[f] * (c - k) + values[c] * (k - f)


def mean(values):
    values = list(values)
    return sum(values) / len(values) if values else 0.0
