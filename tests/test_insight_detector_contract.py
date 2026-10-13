"""Ordered public insight records captured before detector extraction.

The fixture originates from v0.9.2's unchanged deterministic implementation.
It covers all snapshot checks; drift I/O is characterized separately by the
existing proactive/drift tests. Do not regenerate it to bless output changes.
"""

import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest

import brain.insights.proactive as proactive
from brain.insights import detectors


CASES = json.loads((Path(__file__).parent / "fixtures" / "insight_detector_contract.json").read_text())


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_ordered_insight_records_match_release_baseline(case, monkeypatch):
    monkeypatch.setattr(proactive.settings, "ENVIRONMENT", case["environment"])
    monkeypatch.setattr(proactive.settings, "SELF_DIAGNOSIS_MIN_AVG_RECALL", case["minimum_recall"])
    scan = Mock(return_value=[])
    manager = Mock()
    manager.compute_deltas.return_value = []
    monkeypatch.setattr(proactive, "scan_repository_drift", scan)
    monkeypatch.setattr(proactive, "DriftBaselineManager", Mock(return_value=manager))
    snapshot = deepcopy(case["snapshot"])

    actual = [item.to_public_dict() for item in proactive.deterministic_insights(snapshot)]

    assert actual == case["expected"]
    assert snapshot == case["snapshot"]
    scan.assert_called_once()
    manager.load_baseline.assert_called_once()
    manager.compute_deltas.assert_called_once_with([], manager.load_baseline.return_value)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_snapshot_detectors_work_without_runtime_services(case):
    snapshot = deepcopy(case["snapshot"])
    findings = detectors.operational_insights(snapshot, environment=case["environment"])
    findings.extend(detectors.repository_insights(snapshot))
    findings.extend(detectors.worker_insights(snapshot))
    findings.extend(detectors.retrieval_insights(snapshot, minimum_recall=case["minimum_recall"]))
    findings.extend(detectors.indexing_insights(snapshot))
    findings.extend(detectors.recent_job_insights(snapshot))
    findings.extend(detectors.memory_insights(snapshot))

    expected = [item for item in case["expected"] if item["insight_type"] != "operational_readiness"]
    assert [item.to_public_dict() for item in findings] == expected
    assert snapshot == case["snapshot"]
