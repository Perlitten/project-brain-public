import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import brain.insights.proactive as proactive
from brain.insights.proactive import (
    INSIGHT_STATUS_TRANSITIONS,
    INSIGHT_STATUSES,
    InsightNotFoundError,
    InsightTransitionError,
    allowed_status_transitions,
    deterministic_insights,
    generate_proactive_insights,
    set_insight_status,
)
from brain.workers.tasks import run_proactive_insights


def test_deterministic_insights_detect_embedding_gap():
    snapshot = {
        "services": {
            "postgres": {"status": "healthy"},
            "redis": {"status": "healthy"},
            "neo4j": {"status": "healthy"},
        },
        "counts": {"files": 12, "chunks": 40, "rules": 2, "decisions": 1},
        "embeddings": {
            "missing_embeddings": 3,
            "stale_embeddings": 0,
            "incompatible_embeddings": 0,
            "pgvector_coverage_pct": 92.5,
        },
        "recent_indexing_runs": [{"id": 5, "status": "completed"}],
        "recent_jobs": [],
        "reports": [{"name": "audit.md"}],
    }

    insights = deterministic_insights(snapshot)

    assert any(item.dedupe_key == "retrieval_health:embedding_inventory_not_clean" for item in insights)
    retrieval = next(item for item in insights if item.insight_type == "retrieval_health")
    assert retrieval.severity == "warning"
    assert retrieval.evidence


@pytest.mark.asyncio
async def test_evaluator_report_is_parseable_and_does_not_trigger_missing_quality_warning(
    tmp_path: Path,
):
    from apps.api.helpers import get_latest_eval_metrics
    from brain.context.evaluator import GoldenTaskEvaluator

    golden_path = tmp_path / "golden.yaml"
    golden_path.write_text(
        "golden_tasks:\n"
        "  - id: report-contract\n"
        "    description: Verify report contract\n"
        "    expected_files:\n"
        "      - apps/api/helpers.py\n",
        encoding="utf-8",
    )
    reports_path = tmp_path / "reports"
    builder_result = {
        "retrieved_files": [
            {
                "path": "apps/api/helpers.py",
                "trace": {
                    "lexical_match": True,
                    "vector_similarity": 1.0,
                    "graph_relation": False,
                    "cache_match": False,
                },
            }
        ],
        "critic_status": "PASS",
    }

    with (
        patch(
            "brain.context.evaluator.ContextPackBuilder.build_context_pack",
            new=AsyncMock(return_value=builder_result),
        ),
        patch("brain.context.evaluator.reports_dir", return_value=reports_path),
    ):
        result = await GoldenTaskEvaluator(golden_path=golden_path).run_evaluation(tmp_path)

    with patch("apps.api.helpers.reports_dir", return_value=reports_path):
        metrics = get_latest_eval_metrics(tmp_path)

    assert metrics == {
        "avg_precision": result["avg_precision"],
        "avg_recall": result["avg_recall"],
    }

    snapshot = {
        "services": {
            "postgres": {"status": "healthy"},
            "redis": {"status": "healthy"},
            "neo4j": {"status": "healthy"},
        },
        "evaluation": metrics,
    }
    insight_keys = {item.dedupe_key for item in deterministic_insights(snapshot)}
    assert "retrieval_quality:evaluation_missing" not in insight_keys
    assert "retrieval_quality:recall_below_floor" not in insight_keys


@pytest.mark.asyncio
async def test_generate_proactive_insights_uses_deterministic_fallback_without_llm():
    snapshot = {
        "repo": {"path": "/repo", "commit": "abc123"},
        "services": {
            "postgres": {"status": "healthy"},
            "redis": {"status": "healthy"},
            "neo4j": {"status": "healthy"},
        },
        "counts": {"files": 1, "chunks": 1, "rules": 1, "decisions": 1},
        "embeddings": {
            "missing_embeddings": 0,
            "stale_embeddings": 0,
            "incompatible_embeddings": 0,
            "pgvector_coverage_pct": 100.0,
        },
        "recent_indexing_runs": [{"id": 1, "status": "completed"}],
        "recent_jobs": [],
        "reports": [{"name": "audit.md"}],
    }

    with patch("brain.insights.proactive.collect_proactive_snapshot", new=AsyncMock(return_value=snapshot)):
        result = await generate_proactive_insights(use_llm=False, persist=False)

    assert result["llm"]["status"] == "skipped"
    assert result["persisted"]["insights"][0]["dedupe_key"] == "operational_readiness:ready_review"


@pytest.mark.asyncio
async def test_generate_proactive_insights_can_use_mock_llm():
    snapshot = {
        "repo": {"path": "/repo", "commit": "abc123"},
        "services": {
            "postgres": {"status": "healthy"},
            "redis": {"status": "healthy"},
            "neo4j": {"status": "healthy"},
        },
        "counts": {"files": 1, "chunks": 1, "rules": 1, "decisions": 1},
        "embeddings": {
            "missing_embeddings": 0,
            "stale_embeddings": 0,
            "incompatible_embeddings": 0,
            "pgvector_coverage_pct": 100.0,
        },
        "recent_indexing_runs": [{"id": 1, "status": "completed"}],
        "recent_jobs": [],
        "reports": [{"name": "audit.md"}],
    }

    with patch("brain.insights.proactive.collect_proactive_snapshot", new=AsyncMock(return_value=snapshot)):
        result = await generate_proactive_insights(use_llm=True, persist=False)

    assert result["llm"]["status"] == "used"
    assert any(item["dedupe_key"] == "mock_llm_review:provider_mock" for item in result["persisted"]["insights"])
    assert not any(
        item["dedupe_key"] == "llm_diagnostics:stage_unavailable" for item in result["persisted"]["insights"]
    )


@pytest.mark.asyncio
async def test_generate_proactive_insights_reports_timeout_exception_name():
    snapshot = {
        "services": {"postgres": {"status": "healthy"}},
        "counts": {},
        "embeddings": {},
        "recent_indexing_runs": [],
        "recent_jobs": [],
        "reports": [],
    }

    with (
        patch(
            "brain.insights.proactive.collect_proactive_snapshot",
            new=AsyncMock(return_value=snapshot),
        ),
        patch(
            "brain.insights.proactive._llm_insights",
            new=AsyncMock(side_effect=asyncio.TimeoutError()),
        ),
    ):
        result = await generate_proactive_insights(
            use_llm=True,
            persist=False,
            trigger_context={"source": "manual_validation"},
        )

    assert result["llm"]["status"] == "failed"
    assert result["llm"]["error"] == "TimeoutError"
    assert any(
        item["dedupe_key"] == "llm_diagnostics:stage_unavailable"
        for item in result["persisted"]["insights"]
    )


@pytest.mark.asyncio
async def test_trigger_context_is_visible_to_deterministic_and_llm_stages():
    snapshot = {
        "services": {
            "postgres": {"status": "healthy"},
            "redis": {"status": "healthy"},
            "neo4j": {"status": "healthy"},
        },
        "counts": {"files": 1, "chunks": 1, "rules": 1, "decisions": 1},
        "embeddings": {
            "missing_embeddings": 0,
            "stale_embeddings": 0,
            "incompatible_embeddings": 0,
            "pgvector_coverage_pct": 100.0,
        },
        "recent_indexing_runs": [{"id": 1, "status": "completed"}],
        "recent_jobs": [],
        "reports": [{"name": "audit.md"}],
    }
    trigger = {
        "source": "job_failure",
        "summary": "Weekly benchmark failed.",
        "reference": "weekly:42",
    }

    with patch(
        "brain.insights.proactive.collect_proactive_snapshot",
        new=AsyncMock(return_value=snapshot),
    ):
        result = await generate_proactive_insights(
            use_llm=False,
            persist=False,
            trigger_context=trigger,
        )

    assert result["snapshot_summary"]["trigger"] == trigger
    assert any(item["insight_type"] == "automation_trigger" for item in result["persisted"]["insights"])


@pytest.mark.asyncio
async def test_scheduled_trigger_does_not_claim_an_automation_failure():
    snapshot = {
        "services": {
            "postgres": {"status": "healthy"},
            "redis": {"status": "healthy"},
            "neo4j": {"status": "healthy"},
        },
        "counts": {"files": 1, "chunks": 1, "rules": 1, "decisions": 1},
        "embeddings": {
            "missing_embeddings": 0,
            "stale_embeddings": 0,
            "incompatible_embeddings": 0,
            "pgvector_coverage_pct": 100.0,
        },
        "recent_indexing_runs": [{"id": 1, "status": "completed"}],
        "recent_jobs": [],
        "reports": [{"name": "audit.md"}],
    }
    trigger = {
        "source": "scheduler",
        "summary": "Nightly diagnostic scan.",
        "reference": "nightly:42",
    }

    with patch(
        "brain.insights.proactive.collect_proactive_snapshot",
        new=AsyncMock(return_value=snapshot),
    ):
        result = await generate_proactive_insights(
            use_llm=False,
            persist=False,
            trigger_context=trigger,
        )

    assert result["snapshot_summary"]["trigger"] == trigger
    assert not any(
        item["insight_type"] == "automation_trigger"
        for item in result["persisted"]["insights"]
    )


@pytest.mark.asyncio
async def test_unchanged_snapshot_reuses_llm_diagnosis_cache():
    snapshot = {
        "services": {
            "postgres": {"status": "healthy"},
            "redis": {"status": "healthy"},
            "neo4j": {"status": "healthy"},
        },
        "counts": {"files": 1, "chunks": 1, "rules": 1, "decisions": 1},
        "embeddings": {
            "missing_embeddings": 0,
            "stale_embeddings": 0,
            "incompatible_embeddings": 0,
            "pgvector_coverage_pct": 100.0,
        },
        "recent_indexing_runs": [{"id": 1, "status": "completed"}],
        "recent_jobs": [],
        "reports": [{"name": "audit.md"}],
    }

    class Cache:
        def __init__(self):
            self.values = {}

        async def get(self, key):
            return self.values.get(key)

        async def set(self, key, value, ex=None):
            self.values[key] = value
            return True

    cache = Cache()
    llm = AsyncMock(
        return_value=(
            [],
            {
                "status": "used",
                "model": "test",
                "count": 0,
                "input_chars": 100,
            },
        )
    )
    with (
        patch(
            "brain.insights.proactive.collect_proactive_snapshot",
            new=AsyncMock(return_value=snapshot),
        ),
        patch("brain.insights.proactive._llm_insights", new=llm),
        patch("brain.insights.proactive.redis_client", cache),
        patch(
            "brain.insights.proactive.settings.PROACTIVE_INSIGHTS_LLM_CACHE_TTL_SECONDS",
            3600,
        ),
    ):
        first = await generate_proactive_insights(use_llm=True, persist=False)
        second = await generate_proactive_insights(use_llm=True, persist=False)

    assert first["llm"]["status"] == "used"
    assert second["llm"]["status"] == "cache_hit"
    assert second["llm"]["saved_llm_call"] is True
    assert llm.await_count == 1


@pytest.mark.asyncio
async def test_recovered_llm_output_is_not_cached_and_is_operator_visible():
    snapshot = {
        "services": {"postgres": {"status": "healthy"}},
        "counts": {},
        "embeddings": {},
        "recent_indexing_runs": [],
        "recent_jobs": [],
        "reports": [],
    }
    llm = AsyncMock(
        return_value=(
            [],
            {
                "status": "used",
                "model": "test",
                "count": 0,
                "input_chars": 100,
                "parse_recovered": True,
            },
        )
    )
    store_cache = AsyncMock()

    with (
        patch(
            "brain.insights.proactive.collect_proactive_snapshot",
            new=AsyncMock(return_value=snapshot),
        ),
        patch("brain.insights.proactive._load_llm_cache", new=AsyncMock(return_value=None)),
        patch("brain.insights.proactive._llm_insights", new=llm),
        patch("brain.insights.proactive._store_llm_cache", new=store_cache),
    ):
        result = await generate_proactive_insights(use_llm=True, persist=False)

    store_cache.assert_not_awaited()
    assert result["llm"]["parse_recovered"] is True
    assert any(
        item["dedupe_key"] == "llm_diagnostics:truncated_output"
        for item in result["persisted"]["insights"]
    )


def test_llm_cache_ignores_volatile_stale_ages():
    base = {
        "services": {"postgres": {"status": "healthy"}},
        "harness": {
            "task_count": 1,
            "status_counts": {"running": 1},
            "stale_task_count": 1,
            "expired_lease_count": 1,
            "stale_tasks": [
                {
                    "id": "task-1",
                    "status": "running",
                    "age_seconds": 100,
                    "timeout_seconds": 60,
                    "repo_path": "/app",
                }
            ],
        },
        "worker_queue": {
            "queued": 0,
            "processing": 1,
            "retrying": 0,
            "stale_job_count": 1,
            "stale_jobs": [
                {
                    "id": "job-1",
                    "type": "reindex",
                    "status": "running",
                    "age_seconds": 100,
                    "stale_after_seconds": 60,
                }
            ],
        },
    }
    later = {
        **base,
        "harness": {
            **base["harness"],
            "stale_tasks": [{**base["harness"]["stale_tasks"][0], "age_seconds": 999}],
        },
        "worker_queue": {
            **base["worker_queue"],
            "stale_jobs": [{**base["worker_queue"]["stale_jobs"][0], "age_seconds": 999}],
        },
    }
    assert proactive._llm_cache_fingerprint(base) == proactive._llm_cache_fingerprint(later)


def test_llm_cache_ignores_service_latency_but_tracks_health_changes():
    base = {
        "services": {
            "redis": {"status": "healthy", "ping_ms": 34.4},
            "postgres": {"status": "healthy"},
        }
    }
    jitter = {
        "services": {
            "redis": {"status": "healthy", "ping_ms": 10.4},
            "postgres": {"status": "healthy"},
        }
    }
    degraded = {
        "services": {
            "redis": {"status": "degraded", "ping_ms": 10.4, "error": "timeout"},
            "postgres": {"status": "healthy"},
        }
    }

    assert proactive._llm_cache_fingerprint(base) == proactive._llm_cache_fingerprint(jitter)
    assert proactive._llm_cache_fingerprint(base) != proactive._llm_cache_fingerprint(degraded)


def test_llm_grounding_requires_matching_field_label_and_rejects_invented_path():
    snapshot = {"counts": {"files": 10}, "llm_route": {"provider": "mock"}}
    snapshot_json = proactive._compact_json(snapshot, max_chars=1000)
    wrong_label = proactive.InsightCandidate(
        insight_type="test",
        severity="warning",
        title="Claim",
        summary="Claim",
        evidence=[{"label": "unrelated", "value": 10}],
        recommended_action="Review.",
        confidence="high",
        dedupe_key="test:wrong-label",
        source="llm",
    )
    invented_path = proactive.InsightCandidate(
        insight_type="test",
        severity="warning",
        title="Claim",
        summary="The problem is in /invented/path.py.",
        evidence=[{"label": "files", "value": 10}],
        recommended_action="Review.",
        confidence="high",
        dedupe_key="test:path",
        source="llm",
    )
    assert proactive._candidate_evidence_is_grounded(wrong_label, snapshot, snapshot_json) is False
    assert proactive._candidate_evidence_is_grounded(invented_path, snapshot, snapshot_json) is False


def test_parse_json_payload_accepts_fenced_and_prefixed_json():
    fenced = '```json\n{"insights":[]}\n```'
    prefixed = 'Result follows:\n{"insights":[]}\nEnd.'

    assert proactive._parse_json_payload_with_status(fenced) == ({"insights": []}, False)
    assert proactive._parse_json_payload_with_status(prefixed) == ({"insights": []}, False)


def test_parse_json_payload_recovers_only_complete_items_from_truncated_output():
    complete = {
        "insight_type": "service_health",
        "severity": "warning",
        "title": "Postgres is unhealthy",
        "summary": "The snapshot reports an unhealthy Postgres service.",
        "evidence": [{"label": "status", "value": "unhealthy"}],
        "recommended_action": "Inspect Postgres health.",
        "confidence": "high",
    }
    raw = '{"insights":[' + proactive.json.dumps(complete) + ',{"insight_type":"truncated"'

    payload, recovered = proactive._parse_json_payload_with_status(raw)

    assert recovered is True
    assert payload == {"insights": [complete]}


def test_parse_json_payload_recovers_complete_items_from_truncated_bare_list():
    complete = {"insight_type": "service_health"}
    raw = "[" + proactive.json.dumps(complete) + ',{"insight_type":"truncated"'

    payload, recovered = proactive._parse_json_payload_with_status(raw)

    assert recovered is True
    assert payload == {"insights": [complete]}


def test_parse_json_payload_rejects_truncation_before_first_complete_item():
    with pytest.raises(proactive.json.JSONDecodeError):
        proactive._parse_json_payload('{"insights":[{"insight_type":"truncated"')


@pytest.mark.asyncio
async def test_llm_insights_reports_bounded_parse_recovery_and_keeps_grounding():
    snapshot = {"services": {"postgres": {"status": "unhealthy"}}}
    complete = {
        "insight_type": "service_health",
        "severity": "warning",
        "title": "Postgres is unhealthy",
        "summary": "The snapshot reports an unhealthy Postgres service.",
        "evidence": [{"label": "status", "value": "unhealthy"}],
        "recommended_action": "Inspect Postgres health.",
        "confidence": "high",
    }
    provider = SimpleNamespace(
        generate=AsyncMock(
            return_value='{"insights":['
            + proactive.json.dumps(complete)
            + ',{"insight_type":"truncated"'
        )
    )
    router = SimpleNamespace(
        task_model=lambda _kind: "test-insight-model",
        llm=lambda _kind: provider,
    )

    with patch.object(proactive, "get_model_router", return_value=router):
        insights, status = await proactive._llm_insights(snapshot)

    assert [item.title for item in insights] == ["Postgres is unhealthy"]
    assert status["status"] == "used"
    assert status["parse_recovered"] is True
    assert status["count"] == 1
    provider.generate.assert_awaited_once()


@pytest.mark.asyncio
async def test_llm_insights_drops_ungrounded_item_recovered_from_truncation():
    snapshot = {"services": {"postgres": {"status": "unhealthy"}}}
    ungrounded = {
        "insight_type": "service_health",
        "severity": "warning",
        "title": "Invented outage",
        "summary": "An invented service is unavailable.",
        "evidence": [{"label": "status", "value": "invented"}],
        "recommended_action": "Inspect the invented service.",
        "confidence": "high",
    }
    provider = SimpleNamespace(
        generate=AsyncMock(
            return_value='{"insights":['
            + proactive.json.dumps(ungrounded)
            + ',{"insight_type":"truncated"'
        )
    )
    router = SimpleNamespace(
        task_model=lambda _kind: "test-insight-model",
        llm=lambda _kind: provider,
    )

    with patch.object(proactive, "get_model_router", return_value=router):
        insights, status = await proactive._llm_insights(snapshot)

    assert insights == []
    assert status["parse_recovered"] is True
    assert status["count"] == 0


@pytest.mark.asyncio
async def test_persistence_outage_keeps_diagnostic_candidates_for_alerting():
    snapshot = {
        "services": {"postgres": {"status": "unhealthy", "error": "down"}},
        "counts": {},
        "embeddings": {},
        "recent_indexing_runs": [],
        "recent_jobs": [],
        "reports": [],
    }
    with (
        patch(
            "brain.insights.proactive.collect_proactive_snapshot",
            new=AsyncMock(return_value=snapshot),
        ),
        patch(
            "brain.insights.proactive.persist_insights",
            new=AsyncMock(side_effect=RuntimeError("postgres down")),
        ),
    ):
        result = await generate_proactive_insights(use_llm=False, persist=True)

    assert result["persisted"]["error"] == "postgres down"
    assert result["persisted"]["insights"]


@pytest.mark.asyncio
async def test_scheduled_worker_job_skips_when_disabled():
    with (
        patch("brain.workers.tasks.init_db", new=AsyncMock()),
        patch(
            "brain.workers.tasks.settings.PROACTIVE_INSIGHTS_ENABLED",
            False,
        ),
    ):
        result = await run_proactive_insights({"scheduled": True})

    assert result["status"] == "skipped"
    assert result["reason"] == "PROACTIVE_INSIGHTS_ENABLED=false"


# ---------------------------------------------------------------------------
# Insight lifecycle. The vocabulary below is the one persist_insights() writes:
# rows are created "new", anything active that stops recurring is auto-staled,
# and a "stale"/"actioned" row returns to "new" when its dedupe key reappears.
# ---------------------------------------------------------------------------


class _FakeTransaction:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc_info):
        return False


class _FakeSession:
    def __init__(self, row):
        self._row = row

    async def execute(self, statement):
        return SimpleNamespace(scalar_one_or_none=lambda: self._row)

    def begin(self):
        return _FakeTransaction()


def _session_factory_for(row):
    @asynccontextmanager
    async def factory():
        yield _FakeSession(row)

    return factory


def _fake_insight_row(status: str):
    """Only the columns _row_to_public_dict() reads."""
    return SimpleNamespace(
        id=7,
        insight_type="retrieval_health",
        severity="warning",
        title="Semantic retrieval coverage needs repair",
        summary="Some eligible chunks are missing current vectors.",
        evidence=[{"label": "missing_embeddings", "value": "2"}],
        recommended_action="Run the embedding backfill job.",
        confidence="high",
        dedupe_key="retrieval_health:embedding_inventory_not_clean",
        status=status,
        source="deterministic",
        source_model=None,
        engine_version="p0.1",
        occurrence_count=16,
        created_at=None,
        updated_at=None,
        last_seen_at=None,
    )


def test_insight_status_vocabulary_is_the_one_the_engine_writes():
    """No invented status: the lifecycle is exactly the active pair plus the two
    terminal values persist_insights() sets and revives from."""
    assert set(INSIGHT_STATUSES) == set(proactive._ACTIVE_STATUSES) | {"actioned", "stale"}
    assert set(INSIGHT_STATUS_TRANSITIONS) == set(INSIGHT_STATUSES)
    for targets in INSIGHT_STATUS_TRANSITIONS.values():
        assert set(targets).issubset(set(INSIGHT_STATUSES))


@pytest.mark.asyncio
async def test_set_insight_status_performs_legal_transition():
    row = _fake_insight_row("new")
    with patch.object(proactive, "async_session_factory", new=_session_factory_for(row)):
        result = await set_insight_status(7, "accepted")

    assert result["previous_status"] == "new"
    assert result["changed"] is True
    assert result["insight"]["status"] == "accepted"
    assert row.status == "accepted"


@pytest.mark.asyncio
async def test_set_insight_status_is_idempotent():
    row = _fake_insight_row("accepted")
    with patch.object(proactive, "async_session_factory", new=_session_factory_for(row)):
        result = await set_insight_status(7, "accepted")

    assert result["changed"] is False
    assert result["previous_status"] == "accepted"
    assert row.status == "accepted"
    assert row.updated_at is None  # nothing was written


@pytest.mark.asyncio
async def test_set_insight_status_refuses_illegal_transition():
    """Nothing leads out of "stale" by hand — only a recurring finding revives it."""
    row = _fake_insight_row("stale")
    with patch.object(proactive, "async_session_factory", new=_session_factory_for(row)):
        with pytest.raises(InsightTransitionError) as excinfo:
            await set_insight_status(7, "accepted")

    assert "stale" in str(excinfo.value)
    assert row.status == "stale"


@pytest.mark.asyncio
async def test_set_insight_status_refuses_backwards_transition():
    row = _fake_insight_row("actioned")
    with patch.object(proactive, "async_session_factory", new=_session_factory_for(row)):
        with pytest.raises(InsightTransitionError):
            await set_insight_status(7, "accepted")

    assert row.status == "actioned"


@pytest.mark.asyncio
async def test_set_insight_status_rejects_unknown_id():
    with patch.object(proactive, "async_session_factory", new=_session_factory_for(None)):
        with pytest.raises(InsightNotFoundError):
            await set_insight_status(9999, "accepted")


@pytest.mark.asyncio
async def test_set_insight_status_rejects_unknown_status():
    """An invented status never reaches the row."""
    row = _fake_insight_row("new")
    with patch.object(proactive, "async_session_factory", new=_session_factory_for(row)):
        with pytest.raises(InsightTransitionError):
            await set_insight_status(7, "archived")

    assert row.status == "new"


def test_allowed_status_transitions_matches_the_table():
    assert allowed_status_transitions("new") == ("accepted", "actioned", "stale")
    assert allowed_status_transitions("accepted") == ("actioned", "stale")
    assert allowed_status_transitions("stale") == ()
    assert allowed_status_transitions("not-a-status") == ()
