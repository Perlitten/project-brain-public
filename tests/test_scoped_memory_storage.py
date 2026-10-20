"""Actual SQLite/PostgreSQL persistence, with disposable databases/schemas."""
import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from brain.config.settings import settings
from brain.database.models import Decision, Rule
from brain.memory import decision_store, relevance, rule_store
from brain.memory.repo_scope import normalize_repo_scope


@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def memory_store(request, tmp_path, monkeypatch):
    schema = "hardening_" + uuid.uuid4().hex
    if request.param == "sqlite":
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'memory.sqlite3'}")
    else:
        engine = create_async_engine(settings.DATABASE_URL, execution_options={"schema_translate_map": {None: schema}})
    try:
        async with engine.begin() as connection:
            if request.param == "postgres":
                await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            await connection.run_sync(lambda conn: Rule.__table__.create(conn))
            await connection.run_sync(lambda conn: Decision.__table__.create(conn))
    except (OSError, ConnectionError):
        await engine.dispose()
        if (os.environ.get("GITHUB_ACTIONS") or os.environ.get("BRAIN_CI_REQUIRE_SERVICES")):
            raise
        pytest.skip("PostgreSQL unavailable locally")
    factory = async_sessionmaker(engine, expire_on_commit=False, info={"fixture_schema": schema})
    for module in (rule_store, decision_store, relevance):
        monkeypatch.setattr(module, "async_session_factory", factory)
    try:
        yield factory
    finally:
        if request.param == "postgres":
            async with engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await engine.dispose()


@pytest.mark.asyncio
async def test_same_rule_key_cannot_transfer_or_overwrite_other_repo(memory_store):
    first = await rule_store.RuleStore.add_rule("Payment rule", "/RepoA", "A", rule_id="payment")
    second = await rule_store.RuleStore.add_rule("Payment rule", "/RepoB", "B", rule_id="payment")
    global_id = await rule_store.RuleStore.add_rule("Payment rule", None, "global", rule_id="payment")
    assert len({first, second, global_id}) == 3
    assert await rule_store.RuleStore.add_rule("Payment rule", "/RepoB", "B edit", rule_id="payment") == second
    async with memory_store() as session:
        rows = {row.id: row for row in (await session.scalars(select(Rule))).all()}
        assert rows[first].repo_path == "/RepoA" and rows[first].description == "A"
        assert rows[second].description == "B edit" and rows[global_id].repo_path is None
        await session.execute(delete(Rule).where(Rule.id == second))
        await session.commit()
    assert {row.id for row in await rule_store.RuleStore.list_active_rules("/RepoA")} == {first, global_id}


@pytest.mark.asyncio
async def test_existing_bare_rule_id_preserved_only_within_its_scope(memory_store):
    async with memory_store() as session:
        session.add(Rule(id="old-key", name="Old", repo_path="/RepoA", description="A", status="active"))
        await session.commit()
    assert await rule_store.RuleStore.add_rule("Old", "/RepoA", "A edit", rule_id="old-key") == "old-key"
    other = await rule_store.RuleStore.add_rule("Old", "/RepoB", "B", rule_id="old-key")
    assert other != "old-key"
    assert (await rule_store.RuleStore.list_active_rules("/RepoA"))[0].description == "A edit"


@pytest.mark.asyncio
async def test_yaml_rule_sync_cannot_overwrite_another_repository(memory_store, tmp_path):
    first = await rule_store.RuleStore.add_rule("Payment rule", "/RepoA", "A", rule_id="payment")
    path = tmp_path / "rules.yml"
    path.write_text("rules:\n  - id: payment\n    name: Payment rule\n    repo_path: /RepoB\n    description: B\n")
    await rule_store.RuleStore.sync_rules_from_yaml(str(path))
    await rule_store.RuleStore.sync_rules_from_yaml(str(path))
    assert (await rule_store.RuleStore.list_active_rules("/RepoA"))[0].id == first
    assert (await rule_store.RuleStore.list_active_rules("/RepoA"))[0].description == "A"
    assert len(await rule_store.RuleStore.list_active_rules("/RepoB")) == 1


@pytest.mark.asyncio
async def test_global_rule_creation_survives_legacy_repo_owned_bare_id(memory_store):
    async with memory_store() as session:
        session.add(Rule(id="shared", name="Shared", repo_path="/RepoA", description="A", status="active"))
        await session.commit()
    global_id = await rule_store.RuleStore.add_rule("Shared", None, "global", rule_id="shared")
    assert global_id != "shared"
    assert {row.description for row in await rule_store.RuleStore.list_active_rules("/RepoA")} == {"A", "global"}


@pytest.mark.asyncio
async def test_long_rule_keys_with_common_prefix_remain_distinct(memory_store):
    keys = ["x" * 221 + suffix for suffix in ("first", "second")]
    ids = [await rule_store.RuleStore.add_rule("Policy", "/RepoA", key, rule_id=key) for key in keys]
    assert len(set(ids)) == 2 and all(len(value) <= 255 for value in ids)
    assert {row.description for row in await rule_store.RuleStore.list_active_rules("/RepoA")} == set(keys)


@pytest.mark.asyncio
async def test_global_alias_migration_retains_history_and_one_active_policy(memory_store):
    from brain.database.migrations import _ensure_decision_title_uniqueness
    async with memory_store() as session:
        first = Decision(title="Payment policy", repo_path=None, description="Old original content", status="active")
        second = Decision(title="Payment policy", repo_path="system_global", description="Current policy", status="active")
        session.add_all([first, second])
        await session.commit()
        first_id, second_id = first.id, second.id
    engine = memory_store.kw["bind"]
    async with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            # Text SQL intentionally uses only this disposable schema; never
            # include public as a fallback for DROP INDEX/table migration.
            schema = memory_store.kw["info"]["fixture_schema"]
            await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        await _ensure_decision_title_uniqueness(connection)
        await _ensure_decision_title_uniqueness(connection)
    async with memory_store() as session:
        history = await session.get(Decision, first_id)
        assert history.description == "Old original content"
        assert history.status == "superseded" and history.superseded_by_id == second_id
    selected = await relevance.select_relevant_normative_memory("Payment policy", [], "/RepoA")
    assert [row["id"] for row in selected["decisions"]] == [second_id]
    assert await decision_store.DecisionStore.add_decision("Payment policy", None, caller_role="admin") == second_id


@pytest.mark.asyncio
async def test_case_distinct_posix_repositories_keep_independent_decisions(memory_store):
    first = await decision_store.DecisionStore.add_decision("Payment policy", "/CaseRepo", "A")
    second = await decision_store.DecisionStore.add_decision("Payment policy", "/caserepo", "B")
    assert first != second
    a = await relevance.select_relevant_normative_memory("Payment policy", [], "/CaseRepo")
    b = await relevance.select_relevant_normative_memory("Payment policy", [], "/caserepo")
    assert [row["id"] for row in a["decisions"]] == [first]
    assert [row["id"] for row in b["decisions"]] == [second]


@pytest.mark.asyncio
async def test_authorized_relevant_global_decision_reaches_two_repos(memory_store):
    global_id = await decision_store.DecisionStore.add_decision("Payment policy", None, "Payment isolation", caller_role="admin")
    await decision_store.DecisionStore.add_decision("Ban astrology", None, "Unrelated guidance", caller_role="admin")
    owned = await decision_store.DecisionStore.add_decision("Payment policy", "/RepoA", "Private constraint")
    for repo in ("/RepoA", "/RepoB"):
        result = await relevance.select_relevant_normative_memory("Payment policy", [], repo)
        ids = {row["id"] for row in result["decisions"]}
        assert global_id in ids
        assert (owned in ids) is (repo == "/RepoA")
        assert len(ids) == (2 if repo == "/RepoA" else 1)


def test_windows_identity_normalizes_case_without_folding_posix_paths():
    assert normalize_repo_scope("C:\\Projects\\Repo\\") == normalize_repo_scope("c:/projects/repo")
    assert normalize_repo_scope("/CaseRepo") != normalize_repo_scope("/caserepo")


def test_context_cache_does_not_share_case_distinct_repository_payloads():
    from brain.context.context_cache import _make_key
    assert _make_key("/CaseRepo", "payments", "same-sha") != _make_key("/caserepo", "payments", "same-sha")


@pytest.mark.asyncio
async def test_existing_lite_database_gains_canonical_column_without_losing_records(tmp_path):
    from brain.database.migrations import _ensure_decision_title_uniqueness
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'old-lite.sqlite3'}")
    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE TABLE decisions(id INTEGER PRIMARY KEY,title TEXT,repo_path TEXT,status TEXT,description TEXT)"))
            await connection.execute(text("INSERT INTO decisions VALUES(7,'Payment policy',NULL,'active','Original')"))
            await _ensure_decision_title_uniqueness(connection)
            await _ensure_decision_title_uniqueness(connection)
            row = (await connection.execute(text("SELECT id,description,superseded_by_id FROM decisions"))).one()
            assert tuple(row) == (7, "Original", None)
    finally:
        await engine.dispose()
