from __future__ import annotations

import sys

import pytest

from brain.config.settings import settings
from eval import run_paired_lfm_retrieval as runner


def test_fixture_runner_disables_and_restores_shadow_persistence(monkeypatch):
    monkeypatch.setattr(settings, "LATE_INTERACTION_SHADOW_PERSIST_ENABLED", True)
    with runner.fixture_shadow_persistence_disabled():
        assert settings.LATE_INTERACTION_SHADOW_PERSIST_ENABLED is False
    assert settings.LATE_INTERACTION_SHADOW_PERSIST_ENABLED is True


def test_fixture_runner_has_no_shadow_persistence_escape_hatch(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["run_paired_lfm_retrieval.py", "--help"])
    source = runner.PROJECT_ROOT.joinpath(
        "eval", "run_paired_lfm_retrieval.py"
    ).read_text(encoding="utf-8")
    assert "--persist-shadow-events" not in source


def test_fixture_runner_requires_fixed_exact_index_mode(monkeypatch):
    monkeypatch.setattr(settings, "LATE_INTERACTION_DUAL_WRITE_ENABLED", True)
    monkeypatch.setattr(
        settings,
        "LATE_INTERACTION_REMOTE_EXPECTED_INDEX_REVISION",
        "r1294",
    )
    with pytest.raises(RuntimeError, match="DUAL_WRITE"):
        runner.require_exact_evaluation_revision()

    monkeypatch.setattr(settings, "LATE_INTERACTION_DUAL_WRITE_ENABLED", False)
    assert runner.require_exact_evaluation_revision() == "r1294"
