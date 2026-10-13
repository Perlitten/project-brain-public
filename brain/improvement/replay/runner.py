"""Compatibility import for the historical synthetic trajectory fixture.

``IsolatedReplayRunner`` does not execute tasks or measure outcomes. Its
implementation lives in ``historical_fixture`` and warns callers that the
returned metrics are synthetic. Retained for existing import paths; do not
use it as production replay or benchmark evidence.
"""

from brain.improvement.replay.historical_fixture import IsolatedReplayRunner

__all__ = ["IsolatedReplayRunner"]
