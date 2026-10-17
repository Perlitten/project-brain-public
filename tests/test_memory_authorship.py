"""Authorship from the principal: decisions and rules record who wrote them, and
the right to write ``system_global`` follows the caller's role, not an argument.

Store tests run on a disposable sqlite database; the API tests mock the store
and the credential lookup, so no Postgres is needed.
"""
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from brain.auth.principals import ResolvedPrincipal
from brain.database.migrations import _ensure_memory_authorship_columns
from brain.database.models import Decision, Rule
from brain.memory import decision_store, rule_store
from brain.memory.decision_store import MEMORY_ADMIN_SCOPE, caller_role_for
from brain.memory.provenance import MemoryWriteResult

with (
    patch("sqlalchemy.ext.asyncio.create_async_engine"),
    patch("redis.asyncio.from_url"),
    patch("neo4j.AsyncGraphDatabase.driver"),
):
    from apps.api.main import app

from fastapi.testclient import TestClient

client = TestClient(app, raise_server_exceptions=False)
AUTH = {"X-API-Key": "pbk_any"}
ADMIN = ResolvedPrincipal(id=5, name="ops", kind="human", scopes=frozenset({"core:write", MEMORY_ADMIN_SCOPE}))
AGENT = ResolvedPrincipal(id=6, name="bot", kind="service", scopes=frozenset({"core:write"}))


@pytest_asyncio.fixture
async def memory_db(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'memory.sqlite3'}")
    async with engine.begin() as connection:
        await connection.run_sync(lambda conn: Rule.__table__.create(conn))
        await connection.run_sync(lambda conn: Decision.__table__.create(conn))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    for module in (rule_store, decision_store):
        monkeypatch.setattr(module, "async_session_factory", factory)
    try:
        yield factory
    finally:
        await engine.dispose()


def test_role_and_labels_come_from_the_principal():
    assert caller_role_for(ADMIN) == "admin" and caller_role_for(AGENT) == "agent"
    assert caller_role_for(ResolvedPrincipal(id=0, name="legacy-api-key", kind="service")) == "admin"
    assert ADMIN.actor == "principal:5" and ADMIN.author_id == 5
    legacy = ResolvedPrincipal(id=0, name="legacy-api-key", kind="service")
    assert legacy.actor == "principal:legacy-api-key" and legacy.author_id is None


@pytest.mark.asyncio
async def test_store_records_first_author_and_gates_global_by_role(memory_db):
    with pytest.raises(PermissionError, match="elevated admin authorization"):
        await decision_store.DecisionStore.add_decision("Global", None, caller_role="agent", author_principal_id=6)
    first = await decision_store.DecisionStore.add_decision("Global", None, "v1", caller_role="admin",
                                                            author_principal_id=5)
    assert await decision_store.DecisionStore.add_decision("Global", None, "v2", caller_role="admin",
                                                           author_principal_id=9) == first
    scoped = await decision_store.DecisionStore.add_decision("Scoped", "/repo", author_principal_id=6)
    rule = await rule_store.RuleStore.add_rule("Rule", "/repo", "r1", author_principal_id=6)
    assert await rule_store.add_rule("Rule", "/repo", "r2", author_principal_id=9) == rule
    unsigned = await rule_store.RuleStore.add_rule("Unsigned", "/repo")
    async with memory_db() as session:
        decisions = {row.id: row for row in (await session.scalars(select(Decision))).all()}
        rules = {row.id: row for row in (await session.scalars(select(Rule))).all()}
    assert decisions[first].author_principal_id == 5 and decisions[first].description == "v2"
    assert decisions[scoped].author_principal_id == 6
    assert rules[rule].author_principal_id == 6 and rules[rule].description == "r2"
    assert rules[unsigned].author_principal_id is None


@pytest.mark.asyncio
async def test_edits_never_adopt_unattributed_legacy_rows(memory_db):
    """Rows from before authorship (NULL author) stay NULL: an edit records the editor
    in ``updated_by_principal_id`` only, and an identical re-post changes nothing."""
    async with memory_db() as session:
        session.add(Decision(title="Legacy", repo_path="/repo", description="old", status="active"))
        session.add(Rule(id="legacy-rule", name="Legacy rule", repo_path="/repo", description="old",
                         type="convention", severity="medium", status="active"))
        await session.commit()
    rule_fields = {"repo_path": "/repo", "type": "convention", "severity": "medium"}

    assert not (await decision_store.DecisionStore.record_decision(
        "Legacy", "/repo", "old", "active", author_principal_id=5)).changed
    assert not (await rule_store.RuleStore.record_rule(
        "Legacy rule", description="old", author_principal_id=5, **rule_fields)).changed
    edited = await decision_store.DecisionStore.record_decision("Legacy", "/repo", "new", "active",
                                                                author_principal_id=5)
    rule_edit = await rule_store.RuleStore.record_rule("Legacy rule", description="new",
                                                       author_principal_id=5, **rule_fields)
    assert edited.changed and rule_edit.changed

    async with memory_db() as session:
        decisions = (await session.scalars(select(Decision).order_by(Decision.id))).all()
        rules = (await session.scalars(select(Rule).order_by(Rule.id))).all()
    assert [(row.description, row.author_principal_id) for row in decisions] == [("new", None), ("old", None)]
    assert decisions[0].updated_by_principal_id == 5 and decisions[0].version == 2
    canonical_rule = next(row for row in rules if row.id == rule_edit.id)
    history_rule = next(row for row in rules if row.id == rule_edit.superseded_id)
    assert canonical_rule.author_principal_id is None and history_rule.author_principal_id is None
    assert canonical_rule.updated_by_principal_id == 5 and canonical_rule.description == "new"


@pytest.mark.asyncio
async def test_migration_adds_author_columns_to_sqlite_once(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy.sqlite3'}")
    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE TABLE decisions (id INTEGER PRIMARY KEY, title TEXT)"))
            await connection.execute(text("CREATE TABLE rules (id TEXT PRIMARY KEY, name TEXT)"))
            await _ensure_memory_authorship_columns(connection)
            await _ensure_memory_authorship_columns(connection)
            for table in ("decisions", "rules"):
                columns = [row[1] for row in (await connection.execute(text(f"PRAGMA table_info({table})"))).fetchall()]
                assert columns.count("author_principal_id") == 1
    finally:
        await engine.dispose()


def _as(principal):
    return patch("apps.api.auth.resolve_api_key", AsyncMock(return_value=principal))


def test_api_decision_role_and_author_follow_the_principal():
    # The routers write through ``record_decision``/``record_rule`` (version + CAS).
    with _as(AGENT), patch("apps.api.routers.core_memory.DecisionStore.record_decision",
                           AsyncMock(side_effect=PermissionError("admin role required"))) as add:
        # A spoofed role in the body is ignored; the agent's own role decides.
        response = client.post("/decisions", json={"title": "Global", "repo_path": None, "caller_role": "admin"},
                               headers=AUTH)
    assert response.status_code == 403 and response.json()["detail"] == "admin role required"
    assert add.await_args.kwargs["caller_role"] == "agent" and add.await_args.kwargs["author_principal_id"] == 6

    with _as(ADMIN), patch("apps.api.routers.core_memory.DecisionStore.record_decision",
                           AsyncMock(return_value=MemoryWriteResult(id=1, version=1, created=True, changed=True))) as add:
        response = client.post("/decisions", json={"title": "Global", "repo_path": None}, headers=AUTH)
    assert response.status_code == 200 and response.json()["decision_id"] == 1
    assert add.await_args.kwargs["caller_role"] == "admin" and add.await_args.kwargs["author_principal_id"] == 5

    with _as(AGENT), patch("apps.api.routers.core_memory.RuleStore.record_rule",
                           AsyncMock(return_value=MemoryWriteResult(id="r", version=1, created=True, changed=True))) as add:
        response = client.post("/rules", json={"name": "Rule", "repo_path": "/repo"}, headers=AUTH)
    assert response.status_code == 200 and add.await_args.kwargs["author_principal_id"] == 6
