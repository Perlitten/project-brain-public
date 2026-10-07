import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import apps.api.routers.core_status as core_status


def _row(i, severity, status, title="needle"):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=i,
        insight_type="test",
        severity=severity,
        title=title,
        summary="summary",
        evidence=[],
        recommended_action=None,
        confidence="medium",
        status=status,
        source="test",
        source_model=None,
        engine_version="test",
        occurrence_count=1,
        created_at=now,
        last_seen_at=now,
    )


def test_insights_default_excludes_closed_and_facets_before_severity_filter():
    facet = MagicMock()
    facet.all.return_value = [("bad", 2), ("info", 1)]
    total = MagicMock()
    total.scalar.return_value = 1
    rows = MagicMock()
    rows.scalars.return_value.all.return_value = [_row(1, "high", "new")]
    session = AsyncMock()
    session.execute.side_effect = [facet, total, rows]

    @asynccontextmanager
    async def factory():
        yield session

    with patch.object(core_status, "async_session_factory", factory):
        result = asyncio.run(core_status.list_insights(q="needle", severity="bad", page=1, page_size=50))
    assert result["total"] == 1
    assert result["facets"]["severity"] == {"bad": 2, "info": 1}
    assert result["insights"][0]["severity"] == "bad"
    # The SQL facet statement must be issued before the filtered total/page.
    assert session.execute.await_count == 3


def test_insights_unknown_repository_fails_closed():
    result = asyncio.run(core_status.list_insights(repository_id=999, page=2, page_size=10))
    assert result["scope"] == "repository"
    assert result["total"] == 0
    assert result["insights"] == []


def test_insights_real_sql_filters_active_severity_search_and_paging():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from brain.database.models import BrainInsight

    engine = create_engine("sqlite://")
    BrainInsight.__table__.create(engine)
    with Session(engine) as session:
        for i, (severity, status, title) in enumerate([
            ("critical", "new", "needle one"),
            ("warning", "new", "needle two"),
            ("low", "new", "needle three"),
            ("high", "resolved", "needle closed"),
            ("high", "new", "other result"),
        ], 1):
            session.add(BrainInsight(id=i, insight_type="test", severity=severity,
                                    status=status, title=title, summary="evidence",
                                    evidence=[], dedupe_key=str(i)))
        session.commit()

        class AsyncSession:
            async def execute(self, statement):
                return session.execute(statement)

        @asynccontextmanager
        async def factory():
            yield AsyncSession()

        with patch.object(core_status, "async_session_factory", factory):
            result = asyncio.run(core_status.list_insights(q="needle", severity="bad", page_size=1))
            assert result["total"] == 1
            assert [row["id"] for row in result["insights"]] == [1]
            assert result["facets"]["severity"] == {"bad": 1, "warn": 1, "info": 1}
            second = asyncio.run(core_status.list_insights(q="needle", page_size=1, page=2))
            assert second["total"] == 3 and second["total_pages"] == 3
            assert [row["id"] for row in second["insights"]] == [2]
            closed = asyncio.run(core_status.list_insights(status="resolved", severity="bad"))
            assert [row["id"] for row in closed["insights"]] == [4]
    engine.dispose()
