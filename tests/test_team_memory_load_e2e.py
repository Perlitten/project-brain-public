"""Tests for the multi-user team memory load e2e (scripts/team_memory_load_e2e.py).

The pure helpers run everywhere. The stack run needs a disposable PostgreSQL+pgvector server and
Redis and runs only when TEAM_MEMORY_E2E_DATABASE_URL is set (docs/runbooks/team-memory-load-e2e.md)."""
import importlib.util
import ipaddress
import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("team_memory_load_e2e", SCRIPTS / "team_memory_load_e2e.py")
assert spec is not None and spec.loader is not None
load = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = load  # dataclasses resolve annotations through sys.modules
spec.loader.exec_module(load)


def test_percentile_is_nearest_rank():
    assert load.percentile([], 50) is None
    assert load.percentile([3.0], 95) == 3.0
    values = [float(v) for v in range(1, 101)]
    assert load.percentile(values, 50) == 50.0
    assert load.percentile(values, 95) == 95.0
    assert load.percentile(values, 100) == 100.0
    assert load.percentile(reversed(values), 1) == 1.0


def test_latency_summary_reports_milliseconds_and_statuses():
    summary = load.latency_summary({"GET /x": [0.010, 0.020, 0.030]}, {"GET /x": Counter({200: 2, 409: 1})})
    row = summary["GET /x"]
    assert (row["count"], row["p50_ms"], row["max_ms"]) == (3, 20.0, 30.0)
    assert row["statuses"] == {"200": 2, "409": 1}


def test_member_address_is_stable_and_in_test_net_2():
    first = load.member_address("dev_core")
    assert first == load.member_address("dev_core")
    assert ipaddress.ip_address(first) in ipaddress.ip_network("198.51.100.0/24")
    names = [name for name, *_ in load.roster(12)]
    assert len({load.member_address(n) for n in names}) >= len(names) - 1  # one crc32 collision at most


def test_leaked_markers_honours_projects_scope_and_owner():
    text = "mk-core-0123abcd mk-beta-89abcdef pm-alice-00000000 pm-bob-11111111"
    assert load.leaked_markers(text, {"core", "beta"}, "alice") == ["pm-bob-11111111"]
    assert load.leaked_markers(text, {"core"}, "bob") == ["mk-beta-89abcdef", "pm-alice-00000000"]
    assert load.leaked_markers(text, {"core", "beta"}, "alice", scope_project="core") == [
        "mk-beta-89abcdef", "pm-bob-11111111"]
    assert load.leaked_markers("", {"core"}, "alice") == []


def _chain(*rows):
    return [dict(zip(("id", "version", "supersedes_id", "superseded_by_id"), row)) for row in rows]


def test_verify_chain_accepts_a_linear_chain():
    rows = _chain((10, 3, 12, None), (11, 1, None, 10), (12, 2, 11, 10))
    assert load.verify_chain(rows, 10) == []


def test_verify_chain_flags_lost_updates():
    # Two writers both passed the version check: two history rows carry version 2, one is unreachable.
    rows = _chain((10, 3, 12, None), (11, 1, None, 10), (12, 2, 11, 10), (13, 2, 11, 10))
    problems = load.verify_chain(rows, 10)
    assert any("history row(s)" in p for p in problems)
    assert any("not reachable" in p for p in problems)
    assert any("duplicated version number(s) [2]" in p for p in problems)
    assert load.verify_chain(rows, 99) == ["canonical row 99 missing"]


def test_verify_chain_rules_maps_string_ids():
    rows = [{"id": "rule-key", "version": 2, "supersedes_id": "h1", "superseded_by_id": None},
            {"id": "h1", "version": 1, "supersedes_id": None, "superseded_by_id": "rule-key"}]
    assert load.verify_chain_rules(rows, "rule-key") == []
    assert load.verify_chain_rules(rows, "other") == ["canonical rule other missing"]


def test_cas_round_verdict():
    assert load.cas_round_verdict([200, 409, 409])["ok"]
    several = load.cas_round_verdict([200, 200, 409])
    assert not several["ok"] and several["winners"] == 2
    errored = load.cas_round_verdict([200, 500, 409])
    assert not errored["ok"] and errored["other"] == {"500": 1}
    assert errored["losers"] == {"409": 1, "500": 1}
    assert load.cas_round_verdict([200, 409, 409])["losers"] == {"409": 2}


def test_late_write_max_ms_measures_from_revoked_at():
    from datetime import datetime, timedelta, timezone

    revoked = datetime(2026, 10, 18, tzinfo=timezone.utc)
    late = {"learnings": [{"created_at": revoked + timedelta(milliseconds=40)}],
            "decisions": [{"created_at": revoked + timedelta(milliseconds=250)}], "proposals": []}
    assert load.late_write_max_ms(late, revoked) == 250.0
    assert load.late_write_max_ms({"learnings": []}, revoked) is None
    assert load.late_write_max_ms(late, None) is None


def test_markdown_reports_cas_rounds_and_revocation_outcome():
    cas = {"decision_rounds": [{**load.cas_round_verdict([200, 409, 500]), "expected_version": 3,
                                "lost_winners": 0}],
           "rule_rounds": [{**load.cas_round_verdict([200, 200]), "expected_version": 1}],
           "approve_vs_reject": [200, 409], "approve_vs_reject_state": "approved"}
    outcome = {"attempt_statuses": [500, 200], "first_attempt_ok": False, "took_effect": True,
               "revoked_at": "2026-10-18", "requests_after_revoke": 4, "after_revoke_statuses": {"401": 4},
               "honoured_after_revoke": 0, "late_writes": {"learnings": 1}, "revoked_key": {"rest": 401}}
    text = "\n".join(load.render_cas(cas) + load.render_revocation(outcome))
    assert '| decision | 0 | 3 | 1 | `{"409": 1, "500": 1}` | 0 | NO |' in text
    assert "| rule | 0 | 1 | 2 | - | - | NO |" in text
    assert "| approve vs reject (one proposal) | - | 1 | 1 |" in text
    assert "| Revoke attempts (statuses) | `[500, 200]` |" in text
    assert "| First attempt succeeded | NO |" in text
    assert '| Writes dated after revoked_at (0 expected) | `{"learnings": 1}` |' in text
    assert "Not recorded" in "\n".join(load.render_cas({}) + load.render_revocation({}))


def test_roster_extends_the_base_team():
    rows = load.roster(20)
    assert len(rows) == 20 and rows[:len(load.BASE_ROSTER)] == list(load.BASE_ROSTER)
    assert all(set(roles) <= set(load.PROJECTS) for _, _, roles, _ in rows)


def test_port_in_use_detects_a_listener():
    import socket

    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        assert load.port_in_use(port)
    assert not load.port_in_use(port)


def test_parse_args_refuses_small_teams_and_foreign_databases():
    url = "postgresql://postgres:pw@127.0.0.1:5444/postgres"
    assert load.parse_args(["--database-url", url]).users == 12
    with pytest.raises(SystemExit):
        load.parse_args(["--database-url", url, "--users", "5"])
    with pytest.raises(SystemExit):
        load.parse_args(["--database-url", "mysql://u:p@h/db"])


# Isolation and authority invariants that held in every recorded run; the known product defects
# (lost updates, deadlocks, 500 instead of 409) are reported, not asserted here.
ISOLATION_VIOLATIONS = {
    "leak_cross_project_or_personal", "personal_tier_foreign_rows", "context_scope_bleed",
    "foreign_read_not_refused", "foreign_write_not_refused", "reader_wrote_shared_memory",
    "self_approval_not_refused", "approval_without_authority", "g2_single_principal_promoted",
    "request_honoured_after_revoke", "restore_resurrects_revoked_member", "lease_double_grant",
    "lease_overlap_in_time", "harness_exception",
}


@pytest.mark.skipif(not os.getenv("TEAM_MEMORY_E2E_DATABASE_URL"),
                    reason="needs TEAM_MEMORY_E2E_DATABASE_URL (disposable PostgreSQL+pgvector) and Redis")
def test_stack_run_keeps_isolation_invariants(tmp_path):
    out = tmp_path / "run"
    proc = subprocess.run([sys.executable, str(SCRIPTS / "team_memory_load_e2e.py"), "--users", "12", "--ops", "10",
                           "--cas-rounds", "2", "--lease-churn", "4", "--out", str(out)],
                          capture_output=True, text=True, timeout=1800)
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert report["checks"]["errors"] == [], proc.stderr[-2000:]
    skipped_required = [item for item in report["not_exercised"]
                        if item.startswith(("restore fence:", "post-revocation checks:"))]
    assert skipped_required == [], skipped_required
    check_names = [row["name"] for row in report["checks"]["checks"]]
    required_checks = (
        "reader (viewer) cannot write decisions, rules, shared learnings, leases",
        "self-approval, members, readers, reviewers of another project",
        "G2: one principal x8 events over two tasks never becomes active shared memory",
        "lease churn: no two leases on one resource ever overlapped in time",
        "requests sent after the revoke returned: all 401",
        "in-flight writes are fenced: nothing authored by the revoked member is dated after revoked_at",
        "fenced restore: no restored credential honoured",
    )
    missing = [fragment for fragment in required_checks if not any(fragment in name for name in check_names)]
    assert missing == [], {"missing_checks": missing, "checks": check_names}
    assert report["results"]["load"]["ops_done"] > 0
    assert report["results"]["post_revoke_reads"]["ops_done"] > 0
    assert not ISOLATION_VIOLATIONS & set(report["violations"]), report["violations"]
