"""Frozen-holdout and adversarial coverage for the paired retrieval eval."""

import json
import sys
from argparse import Namespace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))
from paired_retrieval_eval import (  # noqa: E402
    DatasetEntry,
    DEFAULT_BASE_DATASET,
    DEFAULT_EXTENSION_DATASET,
    _verify_freeze,
    _write_freeze,
    evaluate_paired,
    load_dataset,
    resolve_evaluation_policy,
    build_evaluation_provenance,
)


def _entry(**overrides):
    base = {
        "id": "q1",
        "question": "where is the thing",
        "expect_files": ["brain/answer.py"],
        "why": "because",
        "language": "en",
        "query_class": "exact_identifier",
        "provenance": {"kind": "curated", "source": "test", "production_real": False},
    }
    base.update(overrides)
    return base


def _write_dataset(path: Path, records: list[dict]) -> Path:
    path.write_text(json.dumps(records), encoding="utf-8")
    return path


def _load(tmp_path: Path, records: list[dict]):
    base = _write_dataset(tmp_path / "a.json", records)
    ext = _write_dataset(tmp_path / "b.json", [_entry(id="q_ext")])
    return load_dataset(base_path=base, extension_path=ext)


def test_split_and_traps_parsed(tmp_path):
    entries = _load(
        tmp_path,
        [
            _entry(split="holdout", query_class="adversarial",
                   trap_files=["brain/decoy.py"], id="q1"),
            _entry(id="q2"),
        ],
    )
    holdout = entries[0]
    assert holdout.split == "holdout"
    assert holdout.trap_files == ("brain/decoy.py",)
    assert entries[1].split == "tune"
    assert entries[1].trap_files == ()


def test_adversarial_requires_trap_files(tmp_path):
    with pytest.raises(ValueError, match="trap_files"):
        _load(tmp_path, [_entry(query_class="adversarial")])


def test_invalid_split_rejected(tmp_path):
    with pytest.raises(ValueError, match="split"):
        _load(tmp_path, [_entry(split="dev")])


def test_freeze_rejects_drifted_dataset(tmp_path):
    dataset = _write_dataset(tmp_path / "holdout_set.json", [_entry()])
    manifest = tmp_path / "frozen_datasets.json"
    _write_freeze(dataset, manifest)
    _verify_freeze(dataset, manifest)  # recorded digest passes
    dataset.write_text(json.dumps([_entry(question="tampered")]), encoding="utf-8")
    with pytest.raises(ValueError, match="frozen dataset drifted"):
        _verify_freeze(dataset, manifest)


def test_freeze_ignores_unrecorded_files(tmp_path):
    dataset = _write_dataset(tmp_path / "other.json", [_entry()])
    _verify_freeze(dataset, tmp_path / "frozen_datasets.json")


def test_real_holdout_loads_and_is_frozen():
    holdout = Path("eval/holdout_set.json")
    assert holdout.exists()
    entries = load_dataset(holdout_path=holdout)
    holdout_entries = [e for e in entries if e.split == "holdout"]
    assert len(holdout_entries) == 12
    assert all(e.trap_files for e in holdout_entries if e.query_class == "adversarial")


def _ranking(*paths):
    return [{"path": p, "score": 1.0} for p in paths]


def _runs(ranking):
    return {"baseline": ranking}


def test_trap_contamination_reported():
    entry = DatasetEntry(
        query_id="q1",
        question="q",
        expected_files=("brain/answer.py",),
        language="en",
        query_class="adversarial",
        provenance={"kind": "curated_synthetic", "source": "test", "production_real": False},
        split="holdout",
        trap_files=("brain/decoy.py",),
    )
    report = evaluate_paired(
        [entry],
        {"q1": _runs(_ranking("brain/decoy.py", "brain/answer.py"))},
    )
    traps = report["per_query"][0]["traps"]["baseline"]
    assert traps["in_top_10"] is True
    assert traps["above_expected"] is True
    assert report["trap_contamination"]["baseline"]["in_top_10_rate"] == 1.0
    assert report["dataset"]["splits"] == {"holdout": 1}


def test_trap_below_expected_not_contaminating():
    entry = DatasetEntry(
        query_id="q1",
        question="q",
        expected_files=("brain/answer.py",),
        language="en",
        query_class="adversarial",
        provenance={"kind": "curated_synthetic", "source": "test", "production_real": False},
        split="tune",
        trap_files=("brain/decoy.py",),
    )
    report = evaluate_paired(
        [entry],
        {"q1": _runs(_ranking("brain/answer.py", "brain/decoy.py"))},
    )
    traps = report["per_query"][0]["traps"]["baseline"]
    assert traps["in_top_10"] is True
    assert traps["above_expected"] is False


@pytest.mark.parametrize("split,holdout", [
    ("holdout", None), ("tune", None), ("all", Path("eval/holdout_set.json")),
])
def test_production_gate_rejects_dataset_selection(split, holdout):
    args = Namespace(
        production_gate=True, base_dataset=DEFAULT_BASE_DATASET,
        extension_dataset=DEFAULT_EXTENSION_DATASET,
        holdout_dataset=holdout, split=split,
        median_ndcg_delta_min=None, improved_ratio_min=None,
        hit3_regression_max=None, slice_regression_max=None,
    )
    with pytest.raises(ValueError, match="production gate"):
        resolve_evaluation_policy(args)


def test_holdout_provenance_records_selection_and_digest(tmp_path):
    runs = tmp_path / "runs.json"
    runs.write_text("{}", encoding="utf-8")
    provenance = build_evaluation_provenance(
        runs_path=runs, base_dataset_path=DEFAULT_BASE_DATASET,
        extension_dataset_path=DEFAULT_EXTENSION_DATASET,
        production_gate=False, thresholds={},
        holdout_dataset_path=Path("eval/holdout_set.json"), split="holdout",
    )
    assert provenance["split"] == "holdout"
    assert provenance["artifacts"]["holdout_dataset"]["sha256"] == (
        "00699ca00deda452eb02cb6b026a102bdd42ebab1a74eb7fb6c5e57f6a28b239"
    )
