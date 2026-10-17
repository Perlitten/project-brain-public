"""Fast unit tests for the pure helpers of the opt-in team e2e check
(scripts/team_e2e_check.py). The check itself needs PostgreSQL, Redis and a
legacy tree and runs only on demand (docs/runbooks/team-e2e-check.md)."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "team_e2e_check.py"
spec = importlib.util.spec_from_file_location("team_e2e_check", SCRIPT)
check = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = check  # dataclasses resolve annotations through sys.modules
spec.loader.exec_module(check)

ADMIN = "postgresql://e2e:pw@127.0.0.1:5448/postgres"


def test_database_guard_only_allows_the_e2e_namespace():
    assert check.e2e_database_url(ADMIN, "brain_e2e_main") == "postgresql://e2e:pw@127.0.0.1:5448/brain_e2e_main"
    for name in ("brain", "postgres", "brain_e2e_x;drop", "brain_e2e_a-b", "e2e_brain_main"):
        with pytest.raises(ValueError, match="Refusing database"):
            check.e2e_database_url(ADMIN, name)
    with pytest.raises(ValueError, match="postgresql://"):
        check.e2e_database_url("mysql://u:p@h/db", "brain_e2e_main")
    with pytest.raises(ValueError, match="query"):
        check.e2e_database_url(ADMIN + "?sslmode=disable", "brain_e2e_main")
    assert check.brain_url("postgresql://u:p@h:1/d") == "postgresql+asyncpg://u:p@h:1/d"


def test_shown_secret_reads_the_line_after_the_label():
    stderr = f"Created member alice\nKey {check.SECRET_LABEL}\n  pbk_example_value  \nDone\n"
    assert check.shown_secret(stderr) == "pbk_example_value"
    with pytest.raises(AssertionError):
        check.shown_secret(f"Key {check.SECRET_LABEL}\n")


def test_redaction_and_report_never_leak_registered_secrets(tmp_path):
    report = check.Report()
    secret = report.secret("pbk_super_secret")
    assert check.redact(f"key={secret}!", report.secrets) == "key=<redacted>!"
    assert report.check("s", "passes", True, {"key": secret})
    assert not report.check("s", "fails", False, f"saw {secret}")
    assert all(secret not in c.evidence for c in report.checks)
    assert report.failed and json.dumps(report.as_dict()).count(secret) == 0


def test_known_gaps_do_not_fail_the_run_and_flag_a_fix():
    report = check.Report()
    report.check("s", "ok", True)
    assert not report.gap("s", "still open", False)
    assert not report.failed
    assert report.gap("s", "fixed upstream", True)
    summary = report.as_dict()
    assert (summary["passed"], summary["failed"], summary["known_gaps"], summary["gaps_fixed"]) == (1, 0, 1, 1)
    assert [c["status"] for c in summary["checks"]] == ["PASS", "GAP", "GAP-FIXED"]


def test_legacy_differences_ignores_new_columns_and_honours_exemptions():
    before = {"decisions": {"1": {"title": "A", "status": "active"}, "2": {"title": "B", "status": "active"}}}
    after = {"decisions": {"1": {"title": "A", "status": "active", "version": 1},
                           "2": {"title": "B", "status": "superseded"}}}
    assert check.legacy_differences(before, after, lambda *_: False) == [
        "decisions[2].status: 'active' -> 'superseded'"]
    assert check.legacy_differences(before, after, lambda table, pk, column: (pk, column) == ("2", "status")) == []
    assert check.legacy_differences(before, {"decisions": {}}, lambda *_: False) == [
        "decisions[1] missing", "decisions[2] missing"]
