"""Exercise paging and freshness against real SQL rather than mocked results."""
import asyncio
from contextlib import asynccontextmanager
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from apps.api.routers import web
from brain.database.models import ContextPack, IndexingRun, Repository


def test_pack_freshness_facets_and_search_cover_all_pages():
    engine = create_engine("sqlite://")
    for model in (Repository, ContextPack, IndexingRun):
        model.__table__.create(engine)
    with Session(engine) as session:
        session.add(Repository(id=3, name="Brain", path="/app"))
        session.add(IndexingRun(repository_id=3, commit_hash="current", status="completed"))
        for index in range(225):
            commit = "current" if index % 3 == 0 else "old" if index % 3 == 1 else None
            session.add(ContextPack(task_description=f"Task {index}", path="missing.md", repository_id=3, repo_commit=commit))
        session.commit()

        class AsyncSession:
            async def execute(self, statement):
                return session.execute(statement)

        @asynccontextmanager
        async def factory():
            yield AsyncSession()

        with patch.object(web, "async_session_factory", factory), patch.object(web, "resolve_context_pack_file", return_value=None):
            fresh = asyncio.run(web.web_context_packs(page=2, page_size=20, status="fresh", repository_id=3))
            assert fresh["total"] == 75
            assert len(fresh["packs"]) == 20
            assert all(pack["stale"] is False for pack in fresh["packs"])
            assert fresh["facets"]["freshness"] == {"fresh": 75, "outdated": 75, "unknown": 75}
            unknown = asyncio.run(web.web_context_packs(page=1, page_size=20, status="unknown", repository_id=3))
            assert unknown["total"] == 75
            assert all(pack["stale"] is None for pack in unknown["packs"])
            match = asyncio.run(web.web_context_packs(page=1, page_size=20, q="Task 224", repository_id=3))
            assert match["total"] == 1
            assert match["packs"][0]["task"] == "Task 224"
            assert match["facets"]["freshness"] == {"fresh": 0, "outdated": 0, "unknown": 1}
    engine.dispose()


def test_index_history_search_facets_respect_query_and_repository():
    engine = create_engine("sqlite://")
    for model in (Repository, IndexingRun):
        model.__table__.create(engine)
    with Session(engine) as session:
        session.add_all([Repository(id=3, name="Brain", path="/app"), Repository(id=4, name="Other", path="/other")])
        session.add_all([
            IndexingRun(id=1, repository_id=3, commit_hash="current", status="completed"),
            IndexingRun(id=2, repository_id=3, commit_hash="old", status="failed"),
            IndexingRun(id=3, repository_id=4, commit_hash="current", status="completed"),
        ])
        session.commit()

        class AsyncSession:
            async def execute(self, statement):
                return session.execute(statement)

            async def get(self, model, key):
                return session.get(model, key)

        @asynccontextmanager
        async def factory():
            yield AsyncSession()

        with patch.object(web, "async_session_factory", factory):
            result = asyncio.run(web.web_index_runs(repository_id=3, page=1, page_size=20, q="current", status="failed"))
            assert result["total"] == 0 and result["runs"] == []
            assert result["facets"] == {"completed": 1}
            by_id = asyncio.run(web.web_index_runs(repository_id=3, page=1, page_size=20, q="2", status=None))
            assert by_id["total"] == 1
            assert by_id["runs"][0]["id"] == 2
            assert by_id["facets"] == {"failed": 1}
    engine.dispose()


def test_memory_filters_normalize_legacy_statuses_before_filtering():
    from brain.database.models import Decision, Rule

    engine = create_engine("sqlite://")
    for model in (Repository, Decision, Rule):
        model.__table__.create(engine)
    with Session(engine) as session:
        session.add_all([
            Decision(id=1, title="n8n old", status="historical"),
            Decision(id=2, title="n8n retired", status="deprecated"),
            Decision(id=3, title="native worker", status="active"),
            Rule(id="rule-1", name="Native", severity="critical", status="active"),
            Rule(id="rule-2", name="Check", severity="medium", status="active"),
            Rule(id="rule-3", name="Old", severity="critical", status="disabled"),
        ])
        session.commit()

        class AsyncSession:
            async def execute(self, statement):
                return session.execute(statement)

        @asynccontextmanager
        async def factory():
            yield AsyncSession()

        with patch.object(web, "async_session_factory", factory):
            result = asyncio.run(web.web_decisions(page=1, page_size=20, q="n8n", status="superseded"))
            assert result["total"] == 2
            assert result["facets"]["status"] == {"superseded": 2}
            accepted = asyncio.run(web.web_decisions(page=1, page_size=20, q=None, status="accepted"))
            assert [row["id"] for row in accepted["decisions"]] == [3]
            assert accepted["facets"]["status"] == {"superseded": 2, "accepted": 1}
            rules = asyncio.run(web.web_rules(page=1, page_size=20, q=None, status=None, severity="block"))
            assert [row["id"] for row in rules["rules"]] == ["rule-1"]
            assert rules["facets"]["severity"] == {"block": 1, "warn": 1}
    engine.dispose()
