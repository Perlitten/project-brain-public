"""The agent-value dashboard must never turn "not measured" into "zero".

Every pack built before composition accounting existed has NULL counts. If the
aggregate read those as zeros, an owner would see "no pack ever carried a rule"
on a system whose packs carry rules in every build — which is exactly the kind
of confident wrong number that makes a dashboard worth less than nothing.
"""

from datetime import datetime, timedelta, timezone

import pytest

from brain.insights import agent_value


class _Result:
    def __init__(self, rows=(), scalar=None):
        self._rows = list(rows)
        self._scalar = scalar

    def all(self):
        return list(self._rows)

    def scalar_one_or_none(self):
        return self._scalar


class _Session:
    """Returns scripted results in the order ``agent_value_metrics`` asks."""

    def __init__(self, script):
        self.script = list(script)
        self.statements = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def execute(self, statement):
        self.statements.append(statement)
        return self.script.pop(0)


NOW = datetime.now(timezone.utc)


def _pack(consumer, tokens, files, rules, decisions, learnings, budget="balanced", at=None):
    return (at or NOW, consumer, tokens, files, rules, decisions, learnings, budget)


def _metrics(monkeypatch, *, packs, pack_start, requests, events, event_start, active):
    session = _Session(
        [
            _Result(rows=packs),
            _Result(scalar=pack_start),
            _Result(rows=requests),
            _Result(rows=events),
            _Result(scalar=event_start),
            _Result(scalar=active),
        ]
    )
    monkeypatch.setattr(agent_value, "async_session_factory", lambda: session)
    return session


@pytest.mark.asyncio
async def test_unrecorded_pack_composition_is_excluded_not_counted_as_empty(monkeypatch):
    packs = [
        _pack("mcp:claude-code", 8000, 12, 2, 1, 0),
        _pack("mcp:claude-code", 6000, 9, 0, 0, 0),
        # Built before accounting shipped: every count is NULL.
        _pack(None, None, None, None, None, None, budget=None),
        _pack(None, None, None, None, None, None, budget=None),
    ]
    _metrics(monkeypatch, packs=packs, pack_start=NOW - timedelta(days=2), requests=[], events=[], event_start=None, active=5)

    result = await agent_value.agent_value_metrics(period_days=30)
    section = result["packs"]

    assert section["total"] == 4
    assert section["measured"] == 2
    assert section["unmeasured"] == 2
    # One of the two measured packs carried memory — the two unmeasured ones
    # must not drag the share down to 1/4.
    assert section["with_memory"] == 1
    assert section["memory_reach"] == 0.5
    assert section["tokens"]["sample_count"] == 2
    assert section["tokens"]["p50"] == 6000.0
    assert section["attributed"] == 2
    assert section["unattributed"] == 2


@pytest.mark.asyncio
async def test_memory_reach_is_null_when_nothing_was_measured(monkeypatch):
    packs = [_pack(None, None, None, None, None, None, budget=None)]
    _metrics(monkeypatch, packs=packs, pack_start=None, requests=[], events=[], event_start=None, active=3)

    section = (await agent_value.agent_value_metrics(period_days=30))["packs"]

    assert section["measured"] == 0
    assert section["with_memory"] is None
    assert section["memory_reach"] is None
    assert section["carrying"] == {"rules": None, "decisions": None, "learnings": None}
    assert section["measurement_started_at"] is None
    assert section["partial"] is True


@pytest.mark.asyncio
async def test_rules_report_no_usage_rather_than_zero_before_measurement(monkeypatch):
    _metrics(monkeypatch, packs=[], pack_start=None, requests=[], events=[], event_start=None, active=7)

    rules = (await agent_value.agent_value_metrics(period_days=30))["rules"]

    assert rules["active"] == 7
    assert rules["measurement_started_at"] is None
    # Seven rules and nothing watching: "7 unused" would be a fabrication.
    assert rules["unused"] is None
    assert rules["delivered_rules"] is None
    assert rules["flagged_rules"] is None


@pytest.mark.asyncio
async def test_rule_usage_separates_reach_from_efficacy(monkeypatch):
    events = [
        ("no-apps-import", "delivered", NOW),
        ("no-apps-import", "delivered", NOW),
        ("no-apps-import", "flagged", NOW),
        ("golden-tasks-contract", "delivered", NOW),
    ]
    _metrics(monkeypatch, packs=[], pack_start=None, requests=[], events=events, event_start=NOW - timedelta(days=3), active=4)

    rules = (await agent_value.agent_value_metrics(period_days=30))["rules"]

    assert rules["delivered_events"] == 3
    assert rules["flagged_events"] == 1
    assert rules["delivered_rules"] == 2
    assert rules["flagged_rules"] == 1
    # Two of four active rules were delivered; the rest reached nobody.
    assert rules["unused"] == 2
    # A rule that caught something ranks above one that was merely delivered.
    assert rules["top"][0]["rule_id"] == "no-apps-import"
    assert rules["top"][0] == {"rule_id": "no-apps-import", "delivered": 2, "flagged": 1}


@pytest.mark.asyncio
async def test_agents_are_ranked_with_unattributed_last(monkeypatch):
    requests = [
        ("mcp:claude-code", "context", "success", "mcp", NOW),
        ("mcp:claude-code", "context", "success", "mcp", NOW - timedelta(hours=1)),
        ("mcp:claude-code", "search", "error", "mcp", NOW - timedelta(hours=2)),
        ("api:unattributed", "search", "success", "http", NOW - timedelta(hours=3)),
        ("api:unattributed", "search", "success", "http", NOW - timedelta(hours=4)),
        ("api:unattributed", "search", "success", "http", NOW - timedelta(hours=5)),
        ("api:unattributed", "search", "success", "http", NOW - timedelta(hours=6)),
    ]
    packs = [_pack("mcp:claude-code", 7000, 10, 1, 0, 0)]
    _metrics(monkeypatch, packs=packs, pack_start=NOW - timedelta(days=1), requests=requests, events=[], event_start=None, active=0)

    agents = (await agent_value.agent_value_metrics(period_days=30))["agents"]

    assert [a["agent"] for a in agents] == ["mcp:claude-code", "unattributed"]
    named = agents[0]
    assert named["requests"] == 3
    assert named["context_requests"] == 2
    assert named["failures"] == 1
    assert named["packs"] == 1
    assert named["packs_with_memory"] == 1
    # One durable pack out of two context requests, not one out of three.
    assert named["pack_rate"] == 0.5
    assert named["surfaces"] == ["mcp"]
    # A caller with no pack has no pack rate to report.
    assert agents[1]["pack_rate"] is None
    assert agents[1]["packs_with_memory"] is None


@pytest.mark.asyncio
async def test_aggregation_refuses_to_silently_sample(monkeypatch):
    packs = [_pack("mcp:claude-code", 1, 1, 0, 0, 0)] * (agent_value.ROW_CAP + 1)
    _metrics(monkeypatch, packs=packs, pack_start=NOW, requests=[], events=[], event_start=None, active=0)

    with pytest.raises(RuntimeError, match="aggregation capacity"):
        await agent_value.agent_value_metrics(period_days=30)


@pytest.mark.asyncio
async def test_rule_usage_counts_reports_when_counting_began(monkeypatch):
    session = _Session(
        [
            _Result(rows=[("no-apps-import", "delivered", 4), ("no-apps-import", "flagged", 1)]),
            _Result(scalar=NOW - timedelta(days=5)),
        ]
    )
    monkeypatch.setattr(agent_value, "async_session_factory", lambda: session)

    result = await agent_value.rule_usage_counts(["no-apps-import", "unused-rule"], period_days=30)

    assert result["measurement_started_at"] is not None
    assert result["counts"]["no-apps-import"] == {"delivered": 4, "flagged": 1}
    # Absent rather than zeroed: the caller decides how to render "never used".
    assert "unused-rule" not in result["counts"]


@pytest.mark.asyncio
async def test_rule_usage_counts_skips_the_query_for_an_empty_page(monkeypatch):
    def explode():
        raise AssertionError("no session should be opened for zero rule ids")

    monkeypatch.setattr(agent_value, "async_session_factory", explode)

    assert await agent_value.rule_usage_counts([]) == {"measurement_started_at": None, "counts": {}}
