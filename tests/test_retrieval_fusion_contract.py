"""Exact stage outputs captured from v0.9.2 before fusion extraction."""

import json
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

import pytest

from brain.database.models import File
from brain.retrieval.fusion import FusionPolicy, fuse_candidates, prepare_channels, rank_channel_paths
from brain.retrieval.types import ChannelCandidate, QueryRoute


CONTRACT = json.loads((Path(__file__).parent / "fixtures" / "retrieval_fusion_contract.json").read_text())


@pytest.mark.parametrize("case", CONTRACT["cases"], ids=lambda case: case["name"])
def test_complete_fusion_records_match_baseline(case):
    inputs = deepcopy(CONTRACT["inputs"])
    if case.get("empty"):
        inputs = {channel: [] for channel in inputs}
    if case.get("ties"):
        inputs = {channel: [] for channel in inputs}
        inputs["lexical"] = [
            {"item_id": path, "raw_score": 1.0, "rank": 1} for path in ("brain/two.py", "brain/one.py")
        ]
    if case.get("strong_graph"):
        inputs["graph"][0]["rank"] = 1
    channels = {
        channel: [ChannelCandidate(channel=channel, **record) for record in rows]
        for channel, rows in inputs.items()
    }
    files = {file["path"]: File(**file) for file in CONTRACT["files"]}
    prepared = prepare_channels(channels, QueryRoute(case["route"]), card_recall=case["card_recall"])
    policy = FusionPolicy(
        channel_weights=case["channel_weights"], linear_weights=case["linear_weights"],
        vector_multiplier=case["vector_multiplier"], mode=case["mode"],
        v2_enabled=case["v2_enabled"], v6_enabled=case["v6_enabled"],
        expected_surface=case["expected_surface"],
    )
    result = fuse_candidates(
        prepared, channels, files, policy=policy, task_type="refactor",
        task_description="Explain indexer", keywords_lower=["indexer"],
    )

    # Compare every score and record field, in order. Arithmetic order is
    # unchanged, so no numerical tolerance is needed for these CPU fixtures.
    assert [asdict(candidate) for candidate in result] == case["expected"]["ranking"]
    assert prepared.file_ranks == case["expected"]["ranks"]
    assert list(prepared.channels) == case["expected"]["channels"]
    assert rank_channel_paths(prepared, channels) == case["expected"]["paths"]
    assert {channel: [asdict(c) for c in rows] for channel, rows in channels.items()} == (
        CONTRACT["normalizations"][case["normalization"]]
    )
