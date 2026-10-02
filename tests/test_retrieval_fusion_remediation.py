from brain.retrieval.pipeline import _content_match_bonus, _channel_weight, QueryRoute

def test_content_match_bonus_is_bounded_tiebreaker():
    # Summary card content match bonus should be tiny (<= 0.008)
    summary = "Auth policy and authentication middleware for the API"
    keywords = ["auth", "policy", "api"]
    bonus = _content_match_bonus("apps/api/auth.py", summary, keywords)
    assert bonus <= 0.01, f"Bonus {bonus} is too large and will break RRF rank order"

def test_card_channel_weight_is_not_dominant():
    # Card channel weight should be moderate (< 1.0) to prevent summary card override
    card_weight = _channel_weight(QueryRoute.EXACT_IDENTIFIER, "card")
    assert card_weight <= 0.9
