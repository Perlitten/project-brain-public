"""The campaign must not turn partial runs, crashes or missing modules into a pass."""

import json

import pytest

from eval.project_mutation import assign_shards, candidate_hash, outcome, summarize


def test_sharding_preserves_the_entire_inventory_and_is_reproducible():
    modules = [{"module": f"brain/{name}.py", "candidates": count}
               for name, count in (("auth", 101), ("store", 70), ("cli", 25), ("models", 0))]
    first = assign_shards(modules, 3)
    assert assign_shards(list(reversed(modules)), 3) == first
    flattened = [row for shard in first for row in shard]
    assert sorted(flattened, key=lambda row: row["module"]) == sorted(modules, key=lambda row: row["module"])
    assert sum(row["candidates"] for row in flattened) == 196
    with pytest.raises(ValueError, match="positive"):
        assign_shards(modules, 0)


def test_report_keeps_timeouts_in_denominator_and_rejects_incomplete_scores(tmp_path):
    codes = {f"brain.example.f__mutmut_{index}": code
             for index, code in enumerate((1, 1, 0, 36, 3, None))}
    module = {"module": "example.py", "candidates": len(codes), "candidate_keys_sha256": candidate_hash(codes)}
    path = tmp_path / "example.py.meta"
    path.write_text(json.dumps({"exit_code_by_key": codes}))
    report = summarize([module], tmp_path)["overall"]
    assert report["killed"] == 2 and report["timed_out"] == 1
    assert report["inconclusive"] == 1 and report["pending"] == 1
    assert not report["complete"] and report["score"] is None

    codes["brain.example.f__mutmut_4"] = 0
    codes["brain.example.f__mutmut_5"] = 1
    path.write_text(json.dumps({"exit_code_by_key": codes}))
    report = summarize([module], tmp_path)["overall"]
    assert report["complete"] and report["score"] == 0.5
    assert report["total"] == 6 and report["timed_out"] == 1
    assert report["equivalent"] == report["type_invalid"] == 0

    path.unlink()
    missing = summarize([module], tmp_path)["overall"]
    assert missing["total"] == missing["pending"] == 6
    assert not missing["complete"] and missing["score"] is None


def test_report_rejects_changed_candidate_identity_and_noninteger_exit_codes(tmp_path):
    codes = {"brain.example.f__mutmut_1": 1}
    module = {"module": "example.py", "candidates": 1, "candidate_keys_sha256": candidate_hash(codes)}
    (tmp_path / "example.py.meta").write_text(json.dumps({"exit_code_by_key": {"different": 1}}))
    with pytest.raises(ValueError, match="identities changed"):
        summarize([module], tmp_path)
    with pytest.raises(ValueError, match="integers"):
        outcome(True)
    with pytest.raises(ValueError, match="integers"):
        outcome("1")
    assert outcome(37) == outcome(3) == outcome(-11) == "inconclusive"
