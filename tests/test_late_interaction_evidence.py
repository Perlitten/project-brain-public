from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from brain.late_interaction.evidence import load_retained_lfm_evidence


def _write_bundle(root: Path) -> tuple[Path, Path]:
    lfm = root / "lfm"
    lfm.mkdir(parents=True)
    report = {
        "dataset": {
            "count": 2,
            "ru_or_ru_en_count": 1,
            "production_real_count": 0,
            "provenance_notice": "Curated fixtures.",
        },
        "evidence": {
            "recall_at_50_eligible_counts": {
                "baseline": 1,
                "lfm_rerank": 1,
            }
        },
        "aggregate": {
            "baseline": {"hit@3": 0.5, "recall@10": 0.4, "ndcg@10": 0.3},
            "lfm_rerank": {"hit@3": 1.0, "recall@10": 0.6, "ndcg@10": 0.5},
        },
        "paired": {
            "lfm_rerank": {
                "mean_deltas": {"ndcg@10": 0.2},
                "median_deltas": {"ndcg@10": 0.1},
                "improved_ndcg_ratio": 0.5,
                "regressed_ndcg_ratio": 0.0,
            }
        },
        "lfm_rerank_gate": {
            "passed": False,
            "checks": {
                "recall_at_50_evidence_complete": False,
                "median_ndcg_delta": True,
            },
        },
        "overall_gate": {"passed": False},
        "evaluation_provenance": {"production_gate": False},
    }
    runs = {
        "repository": {"id": 3, "path": "/app"},
        "complete": False,
        "failures": [{"query_id": "q2"}],
        "results": [
            {
                "late_interaction": {
                    "status": "scored",
                    "latency_ms": 100.0,
                }
            },
            {
                "late_interaction": {
                    "status": "scored",
                    "latency_ms": 900.0,
                }
            },
        ],
    }
    report_path = lfm / "paired-eval-deadbeef-diagnostic.json"
    runs_path = lfm / "paired-rankings-deadbeef.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    runs_path.write_text(json.dumps(runs), encoding="utf-8")
    return report_path, runs_path


def test_retained_lfm_evidence_is_bounded_scoped_and_explicit(tmp_path):
    report_path, _runs_path = _write_bundle(tmp_path)
    observed = datetime.fromtimestamp(report_path.stat().st_mtime, tz=timezone.utc)

    result = load_retained_lfm_evidence(
        tmp_path,
        repository_id=3,
        now=observed,
    )

    assert result["status"] == "ready"
    assert result["kind"] == "diagnostic"
    assert result["stale"] is False
    assert result["quality"]["gate_passed"] is False
    assert result["quality"]["failed_checks"] == [
        "recall_at_50_evidence_complete"
    ]
    assert result["quality"]["recall_at_50_complete"] is False
    assert result["runtime"]["sample_count"] == 2
    assert result["runtime"]["latency_ms"] == {
        "p50": 500.0,
        "p95": 860.0,
        "max": 900.0,
    }
    assert len(result["artifact"]["sha256"]) == 64


def test_retained_lfm_evidence_rejects_wrong_repository(tmp_path):
    _write_bundle(tmp_path)

    result = load_retained_lfm_evidence(tmp_path, repository_id=99)

    assert result == {
        "status": "unavailable",
        "reason": "no_parseable_evidence",
    }


def test_retained_lfm_evidence_requires_rankings_sidecar_for_repository_scope(tmp_path):
    _report_path, runs_path = _write_bundle(tmp_path)
    runs_path.unlink()

    result = load_retained_lfm_evidence(tmp_path, repository_id=3)

    assert result == {
        "status": "unavailable",
        "reason": "no_parseable_evidence",
    }


def test_retained_lfm_evidence_rejects_symlinked_artifact(tmp_path):
    report_path, _runs_path = _write_bundle(tmp_path)
    target = tmp_path / "outside.json"
    target.write_text(report_path.read_text(encoding="utf-8"), encoding="utf-8")
    report_path.unlink()
    try:
        report_path.symlink_to(target)
    except OSError:
        pytest.skip("Creating symlinks is not permitted on this platform")

    result = load_retained_lfm_evidence(tmp_path, repository_id=3)

    assert result == {
        "status": "unavailable",
        "reason": "no_parseable_evidence",
    }
