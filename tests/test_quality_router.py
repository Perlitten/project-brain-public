import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from apps.api.routers.quality import load_quality_evidence


def _write(tmp_path: Path, payload: object) -> None:
    target = tmp_path / "eval" / "quality"
    target.mkdir(parents=True)
    (target / "owner-benchmark-20261008.json").write_text(json.dumps(payload), encoding="utf-8")


def test_quality_evidence_contract_is_readonly_and_source_backed(tmp_path):
    payload = json.loads((Path(__file__).resolve().parents[1] / "eval/quality/owner-benchmark-20261008.json").read_text())
    _write(tmp_path, payload)
    with patch("apps.api.routers.quality.get_repo_root", return_value=tmp_path):
        assert load_quality_evidence() == payload


@pytest.mark.parametrize("payload", [{}, {"schema_version": 2, "source": {}}])
def test_quality_evidence_rejects_incomplete_or_unsupported_schema(tmp_path, payload):
    _write(tmp_path, payload)
    with patch("apps.api.routers.quality.get_repo_root", return_value=tmp_path):
        with pytest.raises(HTTPException) as error:
            load_quality_evidence()
    assert error.value.status_code == 503


def test_quality_evidence_reports_missing_artifact(tmp_path):
    with patch("apps.api.routers.quality.get_repo_root", return_value=tmp_path):
        with pytest.raises(HTTPException) as error:
            load_quality_evidence()
    assert error.value.status_code == 503


@pytest.mark.parametrize("field,value", [("hit5_any_pct", 101), ("mrr_any", float("nan")), ("hit1_any_pct", True)])
def test_quality_evidence_rejects_invalid_metrics(tmp_path, field, value):
    payload = json.loads((Path(__file__).resolve().parents[1] / "eval/quality/owner-benchmark-20261008.json").read_text())
    payload["search"][field] = value
    _write(tmp_path, payload)
    with patch("apps.api.routers.quality.get_repo_root", return_value=tmp_path):
        with pytest.raises(HTTPException) as error:
            load_quality_evidence()
    assert error.value.status_code == 503
