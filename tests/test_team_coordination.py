"""Team coordination (M3): subtask DAG + coordinator, scope overlaps, instruction versions, critical leases and the
current-base integration queue. Runs on SQLite, and on the shared PostgreSQL database when TEST_TEAM_POSTGRES_URL
is set (the ``team_db`` fixture from test_team_foundations)."""
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from brain.auth.principals import ResolvedPrincipal
from brain.database.harness_models import AgentTask, AgentTaskEvent
from brain.database.models import ApiCredential, Decision, Principal, Rule
from brain.team import coordination
from brain.team.activity import ActivityQuery, ActivityScope, list_activity
from brain.team.authorization import TeamConflict, TeamDenied
from brain.team.models import AgentRun, TeamActivityEvent, TeamResourceLease, TeamRunResolution
from brain.team.provisioning import TeamNotFound, revoke_member
from brain.team.store import create_task, request_run
from tests.test_team_foundations import caller, run_payload, task_payload, team_db as team_fixture

team_db = team_fixture
A, B, C = "a" * 40, "b" * 40, "c" * 40


def sub(key, scope=(), deps=(), **extra):
    return dict(title=f"sub {key}", goal="synthetic", scope_paths=list(scope), depends_on=[str(d) for d in deps],
                idempotency_key=f"sub-{key}-0001", **extra)


async def make_parent(session, project, key="parent-0001", who=2, **changes):
    payload = {**task_payload(key), **changes}
    return uuid.UUID((await create_task(session, caller(who), project, payload))["id"])


async def make_run(session, project, task_id, key, who=2, head=None, allowed=None):
    run = await request_run(session, caller(who), project, task_id, run_payload(key, allowed))
    if head:
        await session.execute(update(AgentRun).where(AgentRun.id == uuid.UUID(run["id"])).values(head_sha=head))
    return uuid.UUID(run["id"])


async def set_status(session, task_id, status):
    await session.execute(update(AgentTask).where(AgentTask.id == task_id).values(status=status))


# --------------------------------------------------------------------------- pure helpers


def test_path_normalization_and_overlap():
    assert coordination.normalize_path(" ./src\\api//routes.py ") == "src/api/routes.py"
    for bad in ("", "/etc/passwd", "C:/x", "src/../secret", "..", "a\x00b"):
        with pytest.raises(TeamConflict):
            coordination.normalize_path(bad)
    with pytest.raises(TeamConflict):
        coordination.normalize_resource("src/*.py")
    with pytest.raises(TeamConflict):
        coordination.normalize_resource("resource:Bad Name")
    assert coordination.normalize_resource("resource:db-migrations") == "resource:db-migrations"
    overlap = coordination.paths_overlap
    assert overlap("src/api", "src/api/routes.py") and overlap("src/api/routes.py", "src/api")
    assert overlap("src/**/*.py", "src/api/x.py") and overlap("*.md", "docs/x.md")
    assert not overlap("src/api", "src/apis") and not overlap("docs/*", "src/x.py")
    assert overlap("resource:db", "resource:db") and not overlap("resource:db", "db")


def test_cycle_detection():
    assert coordination.find_cycle({"a": ["b"], "b": ["c"], "c": []}) is None
    assert coordination.find_cycle({"a": ["b"], "b": ["c"], "c": ["a"]}) == ["a", "b", "c", "a"]
    assert coordination.find_cycle({"x": ["x"]}) == ["x", "x"]


# --------------------------------------------------------------------------- subtasks, dependencies, coordinator


@pytest.mark.asyncio
async def test_subtask_dag_dependency_gate_and_dispatch(team_db):
    factory, a, b = team_db
    async with factory() as session, session.begin():
        parent = await make_parent(session, a)
        first = await coordination.create_subtask(session, caller(2), a, parent, sub("one", ["src/one"]))
        s1 = uuid.UUID(first["id"])
        assert first["plan"]["parent_task_id"] == str(parent) and first["branch"] == "main"
        assert await coordination.create_subtask(session, caller(2), a, parent, sub("one", ["src/one"])) == first
        with pytest.raises(TeamConflict):  # same key, different structure
            await coordination.create_subtask(session, caller(2), a, parent, sub("one", ["src/other"]))
        s2 = uuid.UUID((await coordination.create_subtask(session, caller(2), a, parent, sub("two", deps=[s1])))["id"])
        s3 = uuid.UUID((await coordination.create_subtask(session, caller(2), a, parent, sub("three", deps=[s2])))["id"])
        other = await make_parent(session, a, "parent-0002")
        with pytest.raises(TeamConflict, match="same parent"):
            await coordination.create_subtask(session, caller(2), a, parent, sub("bad", deps=[other]))
        with pytest.raises(TeamDenied):  # a Reader cannot plan work
            await coordination.create_subtask(session, caller(1), a, parent, sub("reader"))
        with pytest.raises(TeamDenied):  # another project's member
            await coordination.create_subtask(session, caller(4), a, parent, sub("outsider"))

        view = await coordination.get_plan(session, caller(1), a, parent)  # Readers can see the plan
        assert [item["id"] for item in view["subtasks"]] == [str(s1), str(s2), str(s3)]
        assert view["ready"] == [str(s1)]
        assert {item["id"]: item["state"] for item in view["subtasks"]} == {
            str(s1): "ready", str(s2): "waiting", str(s3): "waiting"}

        with pytest.raises(TeamConflict, match="Dependencies are not done"):
            await request_run(session, caller(2), a, s2, run_payload("early-run-01"))
        with pytest.raises(TeamConflict, match="Dependency cycle"):
            await coordination.update_plan(session, caller(2), a, s1, {"depends_on": [str(s3)], "expected_version": 1})
        with pytest.raises(TeamConflict, match="itself"):
            await coordination.update_plan(session, caller(2), a, s1, {"depends_on": [str(s1)], "expected_version": 1})
        with pytest.raises(TeamConflict, match="version"):
            await coordination.update_plan(session, caller(2), a, s1, {"scope_paths": ["x"], "expected_version": 7})
        with pytest.raises(TeamConflict, match="same parent"):  # a top-level task has no siblings
            await coordination.update_plan(session, caller(2), a, other, {"depends_on": [str(s1)],
                                                                          "expected_version": 0})

        dispatched = await coordination.dispatch(session, caller(2), a, parent, {"idempotency_key": "wave-0001"})
        assert [run["task_id"] for run in dispatched["dispatched"]] == [str(s1)]
        assert dispatched["dispatched"][0]["credential"]
        assert {item["id"]: item["state"] for item in dispatched["plan"]["subtasks"]}[str(s1)] == "running"
        assert (await coordination.dispatch(session, caller(2), a, parent, {"idempotency_key": "wave-0002"}))[
            "dispatched"] == []

        await set_status(session, s1, "completed")
        wave = await coordination.dispatch(session, caller(2), a, parent, {"idempotency_key": "wave-0003"})
        assert [run["task_id"] for run in wave["dispatched"]] == [str(s2)]
        await set_status(session, s2, "cancelled")
        states = {item["id"]: item["state"] for item in (await coordination.plan_view(session, a, parent))["subtasks"]}
        assert states == {str(s1): "done", str(s2): "cancelled", str(s3): "blocked"}


@pytest.mark.asyncio
async def test_subtask_authority_never_exceeds_the_parent(team_db):
    factory, a, _ = team_db
    async with factory() as session, session.begin():
        parent = await make_parent(session, a, permissions=["read"])
        with pytest.raises(TeamDenied):
            await coordination.create_subtask(session, caller(2), a, parent, sub("wide", permissions=["read", "run.request"]))
        narrow = await coordination.create_subtask(session, caller(2), a, parent, sub("narrow"))
        assert (await coordination.plan_view(session, a, parent))["subtasks"][0]["id"] == narrow["id"]


# --------------------------------------------------------------------------- overlap detection


@pytest.mark.asyncio
async def test_scope_overlaps_warn_and_reach_the_activity_feed(team_db):
    factory, a, _ = team_db
    async with factory() as session, session.begin():
        parent = await make_parent(session, a)
        await coordination.update_plan(session, caller(2), a, parent, {"scope_paths": ["src"], "expected_version": 0})
        child = await coordination.create_subtask(session, caller(2), a, parent, sub("api", ["src/api"]))
        assert child["warnings"] == []  # a parent and its own subtask are not flagged
        sibling = await coordination.create_subtask(session, caller(2), a, parent, sub("docs", ["docs/*"]))
        assert sibling["warnings"] == []
        other = await make_parent(session, a, "parent-0002")
        result = await coordination.update_plan(session, caller(3), a, other,
                                                {"scope_paths": ["src/api/routes.py"], "expected_version": 0})
        kinds = sorted((w["kind"], w["other_task_id"]) for w in result["warnings"])
        assert kinds == sorted([("task", str(parent)), ("task", child["id"])])
        assert result["warnings"][0]["paths"]
        # Re-declaring the same scope warns again but records each overlap once.
        again = await coordination.update_plan(session, caller(3), a, other,
                                               {"scope_paths": ["src/api/routes.py"], "expected_version": 1})
        assert len(again["warnings"]) == 2
        rows = (await session.execute(select(TeamActivityEvent).where(
            TeamActivityEvent.kind == "coordination.overlap"))).scalars().all()
        assert len(rows) == 2 and {row.repository_id for row in rows} == {1}
        listed = await coordination.list_overlaps(session, caller(1), a)
        assert len(listed) == 2

        scope = ActivityScope(administrator=False, repository_ids=frozenset({1}), project_ids=frozenset({a}))
        feed = await list_activity(session, scope, ActivityQuery(kinds=frozenset({"coordination"})))
        assert [event["kind"] for event in feed["events"]] == ["coordination.overlap"] * 2
        hidden = ActivityScope(administrator=False, repository_ids=frozenset({2}), project_ids=frozenset())
        assert (await list_activity(session, hidden, ActivityQuery(kinds=frozenset({"coordination"}))))["events"] == []

        # A finished task stops counting.
        await set_status(session, other, "completed")
        assert await coordination.list_overlaps(session, caller(1), a) == [
            item for item in listed if item["kind"] == "task" and str(other) not in (item["task_id"], item["other_task_id"])]


# --------------------------------------------------------------------------- instruction resolution


@pytest.mark.asyncio
async def test_runs_record_instruction_versions_and_flag_superseded_ones(team_db):
    factory, a, _ = team_db
    async with factory() as session, session.begin():
        session.add_all([
            Decision(id=1, title="in A", repo_path="/synthetic/A", status="active", version=1),
            Decision(id=2, title="global", repo_path=None, status="active", version=3),
            Decision(id=3, title="in B", repo_path="/synthetic/B", status="active", version=1),
            Decision(id=4, title="old", repo_path="/synthetic/A", status="deprecated", version=1),
            Rule(id="rule-a", name="rule in A", repo_path="/synthetic/A", status="active", version=2),
            Rule(id="rule-off", name="disabled", repo_path=None, status="disabled", version=1)])
        await session.flush()
        task_id = await make_parent(session, a)
        run_id = await make_run(session, a, task_id, "instr-run-01")
        snapshot = (await session.execute(select(TeamRunResolution).where(TeamRunResolution.run_id == run_id))).scalar_one()
        assert sorted((item["kind"], str(item["id"]), item["version"]) for item in snapshot.resolved) == [
            ("decision", "1", 1), ("decision", "2", 3), ("rule", "rule-a", 2)]
        current = await coordination.get_run_instructions(session, caller(1), a, task_id, run_id)
        assert current["status"] == "current" and current["superseded"] == []

        await session.execute(update(Decision).where(Decision.id == 1).values(version=2))
        await session.execute(update(Rule).where(Rule.id == "rule-a").values(status="disabled"))
        stale = await coordination.get_run_instructions(session, caller(1), a, task_id, run_id)
        assert stale["status"] == "superseded"
        assert sorted((item["kind"], str(item["id"]), item["reason"]) for item in stale["superseded"]) == [
            ("decision", "1", "edited"), ("rule", "rule-a", "retired")]
        with pytest.raises(TeamNotFound):
            await coordination.get_run_instructions(session, caller(1), a, task_id, uuid.uuid4())


# --------------------------------------------------------------------------- critical leases


@pytest.mark.asyncio
async def test_leases_are_exclusive_expiring_and_fenced(team_db):
    factory, a, _ = team_db
    async with factory() as session, session.begin():
        held = (await coordination.acquire_lease(session, caller(2), a, {
            "repository_id": 1, "resource": "src/api", "ttl_seconds": 60}))["lease"]
        assert held["fencing_token"] == 1 and held["state"] == "held" and held["holder_id"] == 2
        again = (await coordination.acquire_lease(session, caller(2), a, {"repository_id": 1, "resource": "src/api"}))
        assert again["lease"]["id"] == held["id"] and again["lease"]["fencing_token"] == 1
        for resource in ("src/api", "src/api/x.py", "src"):
            with pytest.raises(TeamConflict):
                await coordination.acquire_lease(session, caller(3), a, {"repository_id": 1, "resource": resource})
        named = (await coordination.acquire_lease(session, caller(3), a, {
            "repository_id": 1, "resource": "resource:db-migrations"}))["lease"]
        assert named["fencing_token"] == 1
        with pytest.raises(TeamDenied):  # Readers cannot lease
            await coordination.acquire_lease(session, caller(1), a, {"repository_id": 1, "resource": "docs"})
        with pytest.raises(TeamDenied):  # repository of another project
            await coordination.acquire_lease(session, caller(2), a, {"repository_id": 2, "resource": "docs"})
        with pytest.raises(TeamConflict):
            await coordination.acquire_lease(session, caller(2), a, {"repository_id": 1, "resource": "src/*.py"})
        with pytest.raises(TeamConflict):
            await coordination.acquire_lease(session, caller(2), a, {"repository_id": 1, "resource": "x",
                                                                     "ttl_seconds": 5})

        # Expiry lets another member take over; the fencing token moves on and stale holders are refused.
        await session.execute(update(TeamResourceLease).where(TeamResourceLease.id == held["id"]).values(
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
        taken = (await coordination.acquire_lease(session, caller(3), a, {"repository_id": 1, "resource": "src/api"}))
        assert taken["lease"]["id"] == held["id"] and taken["lease"]["fencing_token"] == 2
        assert taken["lease"]["holder_id"] == 3
        for action in (coordination.renew_lease, coordination.release_lease):
            with pytest.raises(TeamConflict, match="fencing token"):
                await action(session, caller(2), a, held["id"], {"fencing_token": 1})
        with pytest.raises(TeamConflict):
            await coordination.renew_lease(session, caller(3), a, held["id"], {"fencing_token": 1})
        renewed = await coordination.renew_lease(session, caller(3), a, held["id"], {"fencing_token": 2,
                                                                                      "ttl_seconds": 600})
        assert renewed["fencing_token"] == 2
        released = await coordination.release_lease(session, caller(3), a, held["id"], {"fencing_token": 2})
        assert released["state"] == "released" and released["released_reason"] == "released"
        with pytest.raises(TeamNotFound):
            await coordination.release_lease(session, caller(3), a, 9999, {"fencing_token": 1})
        live = await coordination.list_leases(session, caller(1), a)
        assert [lease["resource"] for lease in live] == ["resource:db-migrations"]
        assert len(await coordination.list_leases(session, caller(1), a, include_inactive=True)) == 2
        reacquired = (await coordination.acquire_lease(session, caller(2), a, {"repository_id": 1, "resource": "src/api"}))
        assert reacquired["lease"]["fencing_token"] == 3


@pytest.mark.asyncio
async def test_lease_overlap_warns_and_revocation_releases_member_and_run_leases(team_db):
    factory, a, _ = team_db
    async with factory() as session, session.begin():
        task_id = await make_parent(session, a)
        await coordination.update_plan(session, caller(2), a, task_id, {"scope_paths": ["src"], "expected_version": 0})
        other = await make_parent(session, a, "parent-0002", who=3)
        acquired = await coordination.acquire_lease(session, caller(3), a, {
            "repository_id": 1, "resource": "src/core", "task_id": str(other)})
        assert [w["task_id"] for w in acquired["warnings"]] == [str(task_id)]
        own = await coordination.acquire_lease(session, caller(2), a, {"repository_id": 1, "resource": "docs"})
        # A run of member 2 leases through its own credential (holder = the run's principal).
        run_id = await make_run(session, a, task_id, "lease-run-01", allowed=["read", "run.request"])
        run = (await session.execute(select(AgentRun).where(AgentRun.id == run_id))).scalar_one()
        credential = (await session.execute(select(ApiCredential).where(ApiCredential.id == run.credential_id))).scalar_one()
        run_caller = ResolvedPrincipal(run.principal_id, f"run-{run.principal_id}", "run",
                                       scopes=frozenset(credential.scopes), credential_id=run.credential_id)
        by_run = (await coordination.acquire_lease(session, run_caller, a, {"repository_id": 1,
                                                                             "resource": "resource:cache"}))["lease"]
        assert by_run["holder_id"] == run.principal_id and by_run["task_id"] == str(task_id)

        principal = (await session.execute(select(Principal).where(Principal.id == 2))).scalar_one()
        await revoke_member(session, principal=principal, actor_id=4)
        rows = {row.id: row for row in (await session.execute(select(TeamResourceLease))).scalars().all()}
        for lease in (own["lease"], by_run):
            assert rows[lease["id"]].released_reason == "holder revoked"
            assert rows[lease["id"]].fencing_token == lease["fencing_token"] + 1
        assert rows[acquired["lease"]["id"]].released_at is None  # member 3 keeps its lease
        # The freed resource can be taken immediately, past the token the revocation burned.
        assert (await coordination.acquire_lease(session, caller(3), a, {"repository_id": 1, "resource": "docs"}))[
            "lease"]["fencing_token"] == 3


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv("TEST_TEAM_POSTGRES_URL"), reason="requires a disposable PostgreSQL team database")
async def test_concurrent_lease_acquisitions_have_one_holder(team_db):
    import asyncio

    factory, a, _ = team_db

    async def attempt(who, resource):
        try:
            async with factory() as session, session.begin():
                return (await coordination.acquire_lease(session, caller(who), a, {
                    "repository_id": 1, "resource": resource}))["lease"]["holder_id"]
        except TeamConflict:
            return None

    results = await asyncio.gather(*(attempt(who, resource) for who, resource in
                                     [(2, "src/api"), (3, "src/api/x.py"), (2, "src"), (3, "src/api")]))
    winners = {holder for holder in results if holder is not None}
    assert len(winners) == 1  # overlapping paths never end up held by two members
    async with factory() as session:
        live = (await session.execute(select(TeamResourceLease).where(TeamResourceLease.released_at.is_(None)))).scalars()
        assert {row.holder_id for row in live} == winners


# --------------------------------------------------------------------------- integration queue


@pytest.mark.asyncio
async def test_integration_queue_order_rebase_instructions_and_authority(team_db):
    factory, a, _ = team_db
    async with factory() as session, session.begin():
        parent = await make_parent(session, a)
        s1 = uuid.UUID((await coordination.create_subtask(session, caller(2), a, parent, sub("one")))["id"])
        s2 = uuid.UUID((await coordination.create_subtask(session, caller(2), a, parent, sub("two", deps=[s1])))["id"])
        pending = await make_run(session, a, s1, "queue-run-00")
        with pytest.raises(TeamConflict, match="head revision"):
            await coordination.enqueue(session, caller(2), a, {"run_id": str(pending)})
        r1 = await make_run(session, a, s1, "queue-run-01", head=B)
        r2 = await make_run(session, a, s1, "queue-run-02", head=C)
        with pytest.raises(TeamDenied):
            await coordination.enqueue(session, caller(1), a, {"run_id": str(r1)})
        e1 = await coordination.enqueue(session, caller(2), a, {"run_id": str(r1)})
        assert e1["status"] == "queued" and e1["base_sha"] == A and e1["head_sha"] == B
        assert await coordination.enqueue(session, caller(2), a, {"run_id": str(r1)}) == e1
        e2 = await coordination.enqueue(session, caller(2), a, {"run_id": str(r2)})
        assert [item["id"] for item in await coordination.list_queue(session, caller(1), a)] == [e1["id"], e2["id"]]

        with pytest.raises(TeamDenied):  # Contributors cannot integrate
            await coordination.integrate(session, caller(2), a, e1["id"], {"expected_version": 1})
        with pytest.raises(TeamConflict, match="Earlier queue entry"):
            await coordination.integrate(session, caller(3), a, e2["id"], {"expected_version": 1})
        with pytest.raises(TeamConflict, match="current_base_sha is required"):
            await coordination.integrate(session, caller(3), a, e1["id"], {"expected_version": 1})
        with pytest.raises(TeamConflict, match="version"):
            await coordination.integrate(session, caller(3), a, e1["id"], {"expected_version": 5,
                                                                          "current_base_sha": A})
        with pytest.raises(TeamConflict, match="Dependencies are not done"):
            await request_run(session, caller(2), a, s2, run_payload("dep-run-001"))
        done = await coordination.integrate(session, caller(3), a, e1["id"], {"expected_version": 1,
                                                                             "current_base_sha": A})
        assert done["status"] == "integrated" and done["decided_by"] == 3 and done["checked_base_sha"] == A
        await request_run(session, caller(2), a, s2, run_payload("dep-run-001"))  # integration completes s1
        with pytest.raises(TeamConflict, match="already integrated"):
            await coordination.integrate(session, caller(3), a, e1["id"], {"expected_version": 2})

        # The second run was built on A, but the current base is now B: it needs a rebase (kept on record).
        rebase = await coordination.integrate(session, caller(3), a, e2["id"], {"expected_version": 1})
        assert rebase["status"] == "needs_rebase" and rebase["checked_base_sha"] == B and rebase["version"] == 2
        with pytest.raises(TeamConflict, match="predates"):
            await coordination.integrate(session, caller(3), a, e2["id"], {"expected_version": 2,
                                                                          "current_base_sha": A})

        # Independent integration: a Reviewer cannot integrate their own run.
        own_task = await make_parent(session, a, "parent-0003", who=3)
        own_run = await make_run(session, a, own_task, "queue-run-03", who=3, head=C)
        await session.execute(update(AgentRun).where(AgentRun.id == own_run).values(base_sha=B))
        own = await coordination.enqueue(session, caller(3), a, {"run_id": str(own_run)})
        with pytest.raises(TeamDenied):
            await coordination.integrate(session, caller(3), a, own["id"], {"expected_version": 1})
        assert (await coordination.reject(session, caller(3), a, own["id"], {
            "reason": "withdrawn", "expected_version": 1}))["status"] == "rejected"

        # Instructions changed after the run started: flagged, not integrated.
        session.add(Decision(id=10, title="in A", repo_path="/synthetic/A", status="active", version=1))
        await session.flush()
        r4 = await make_run(session, a, parent, "queue-run-04", head="d" * 40)
        session.add(AgentTaskEvent(task_id=parent, event_type="run.execution.completed", actor="executor",
                                   idempotency_key=f"exec:{r4}:completed", payload_json={"base_sha": B}))
        await session.execute(update(Decision).where(Decision.id == 10).values(version=2))
        e4 = await coordination.enqueue(session, caller(2), a, {"run_id": str(r4)})
        assert e4["base_sha"] == B  # the base the executor actually used
        stale = await coordination.integrate(session, caller(3), a, e4["id"], {"expected_version": 1})
        assert stale["status"] == "stale_instructions"
        assert [(item["id"], item["reason"]) for item in stale["superseded_instructions"]] == [(10, "edited")]
        with pytest.raises(TeamDenied):  # neither a Reviewer nor the member who queued it
            await coordination.reject(session, caller(1), a, e4["id"], {"reason": "no", "expected_version": 2})
        withdrawn = await coordination.reject(session, caller(2), a, e4["id"], {"reason": "redo",
                                                                               "expected_version": 2})
        assert withdrawn["status"] == "rejected" and withdrawn["decided_by"] == 2

        # A run revoked after queueing is rejected on its check.
        r5 = await make_run(session, a, parent, "queue-run-05", head="e" * 40)
        await session.execute(update(AgentRun).where(AgentRun.id == r5).values(base_sha="d" * 40))
        e5 = await coordination.enqueue(session, caller(2), a, {"run_id": str(r5)})
        await session.execute(update(AgentRun).where(AgentRun.id == r5).values(revoked_at=datetime.now(timezone.utc)))
        assert (await coordination.integrate(session, caller(3), a, e5["id"], {"expected_version": 1}))[
            "status"] == "rejected"

        assert [item["id"] for item in await coordination.list_queue(session, caller(1), a)] == [e2["id"]]
        kinds = sorted(row.kind for row in (await session.execute(select(TeamActivityEvent).where(
            TeamActivityEvent.kind.like("coordination.%")))).scalars().all())
        assert kinds == sorted(["coordination.integration_queued"] * 5 + ["coordination.integrated",
                               "coordination.needs_rebase", "coordination.stale_instructions"]
                               + ["coordination.integration_rejected"] * 3)


# --------------------------------------------------------------------------- HTTP and MCP surface


@pytest.mark.asyncio
async def test_http_coordination_routes(team_db, monkeypatch):
    from apps.api.routers import team_coordination
    from tests.test_team_transport import CONTRIBUTOR, READER, client_for, membership_driven, personal_app

    factory, a, _ = team_db
    membership_driven(monkeypatch)
    app = personal_app(factory, monkeypatch)
    app.include_router(team_coordination.router)
    reviewer = {"X-API-Key": "fixture-3"}
    base = f"/team/projects/{a}"
    async with client_for(app) as client:
        parent = (await client.post(f"{base}/tasks", headers=CONTRIBUTOR, json=task_payload("http-parent-01"))).json()
        url = f"{base}/tasks/{parent['id']}"
        first = await client.post(f"{url}/subtasks", headers=CONTRIBUTOR, json=sub("h1", ["src/api"]))
        assert first.status_code == 200, first.text
        second = await client.post(f"{url}/subtasks", headers=CONTRIBUTOR,
                                   json=sub("h2", ["src/api/x.py"], deps=[first.json()["id"]]))
        assert second.json()["warnings"][0]["other_task_id"] == first.json()["id"]
        assert (await client.post(f"{url}/subtasks", headers=READER, json=sub("h3"))).status_code == 403
        assert (await client.post(f"{url}/subtasks", headers=CONTRIBUTOR, json=dict(sub("h4"), x=1))).status_code == 422
        plan = await client.get(f"{url}/plan", headers=READER)
        assert plan.status_code == 200 and plan.json()["ready"] == [first.json()["id"]]
        cycle = await client.put(f"{base}/tasks/{first.json()['id']}/plan", headers=CONTRIBUTOR,
                                 json={"depends_on": [second.json()["id"]], "expected_version": 1})
        assert cycle.status_code == 409 and "cycle" in cycle.json()["detail"]
        assert (await client.put(f"{url}/plan", headers=READER, json={"scope_paths": ["x"],
                                                                       "expected_version": 0})).status_code == 403
        gated = await client.post(f"{base}/tasks/{second.json()['id']}/runs", headers=CONTRIBUTOR,
                                  json=run_payload("http-gated-01"))
        assert gated.status_code == 409 and "Dependencies are not done" in gated.json()["detail"]
        dispatched = await client.post(f"{url}/dispatch", headers=CONTRIBUTOR, json={"idempotency_key": "http-wave-1"})
        [run] = dispatched.json()["dispatched"]
        instructions = await client.get(f"{base}/tasks/{run['task_id']}/runs/{run['id']}/instructions", headers=READER)
        assert instructions.json()["status"] == "current"
        overlaps = await client.get(f"{base}/overlaps", headers=READER, params={"repository_id": 1})
        assert len(overlaps.json()["overlaps"]) == 1

        lease = (await client.post(f"{base}/leases", headers=CONTRIBUTOR,
                                   json={"repository_id": 1, "resource": "resource:deploy"})).json()["lease"]
        busy = await client.post(f"{base}/leases", headers=reviewer, json={"repository_id": 1,
                                                                            "resource": "resource:deploy"})
        assert busy.status_code == 409
        assert (await client.post(f"{base}/leases", headers=READER, json={"repository_id": 1,
                                                                           "resource": "docs"})).status_code == 403
        renewed = await client.post(f"{base}/leases/{lease['id']}/renew", headers=CONTRIBUTOR,
                                    json={"fencing_token": 1, "ttl_seconds": 120})
        assert renewed.status_code == 200
        assert (await client.post(f"{base}/leases/{lease['id']}/release", headers=reviewer,
                                  json={"fencing_token": 1})).status_code == 409
        assert (await client.post(f"{base}/leases/424242/release", headers=CONTRIBUTOR,
                                  json={"fencing_token": 1})).status_code == 404
        assert len((await client.get(f"{base}/leases", headers=READER)).json()["leases"]) == 1
        assert (await client.post(f"{base}/leases/{lease['id']}/release", headers=CONTRIBUTOR,
                                  json={"fencing_token": 1})).json()["state"] == "released"

        async with factory() as session, session.begin():
            await session.execute(update(AgentRun).where(AgentRun.id == uuid.UUID(run["id"])).values(head_sha=B))
        entry = (await client.post(f"{base}/integration", headers=CONTRIBUTOR, json={"run_id": run["id"]})).json()
        assert (await client.post(f"{base}/integration/{entry['id']}/integrate", headers=CONTRIBUTOR,
                                  json={"expected_version": 1, "current_base_sha": A})).status_code == 403
        integrated = await client.post(f"{base}/integration/{entry['id']}/integrate", headers=reviewer,
                                       json={"expected_version": 1, "current_base_sha": A})
        assert integrated.json()["status"] == "integrated"
        queue = await client.get(f"{base}/integration", headers=READER, params={"include_done": "true"})
        assert [item["status"] for item in queue.json()["entries"]] == ["integrated"]
        assert (await client.post(f"{base}/integration/{entry['id']}/reject", headers=reviewer,
                                  json={"reason": "late", "expected_version": 2})).status_code == 409


COORDINATION_READS = ("team_task_plan", "team_coordination_overlaps", "team_list_leases", "team_integration_queue")
COORDINATION_WRITES = ("team_claim_lease", "team_renew_lease", "team_release_lease", "team_declare_scope",
                       "team_enqueue_integration")


def test_coordination_mcp_tools_are_registered_with_their_annotations():
    import asyncio

    import apps.mcp_server.team_server  # noqa: F401 - registers the team tools
    from apps.mcp_server.remote_server import mcp

    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    for name in COORDINATION_READS:
        assert tools[name].annotations.read_only_hint is True
    for name in COORDINATION_WRITES:
        assert tools[name].annotations.read_only_hint is False, name
        assert tools[name].annotations.destructive_hint is False, name
        assert tools[name].annotations.idempotent_hint is True, name


@pytest.mark.asyncio
async def test_coordination_mcp_writes_go_through_the_api_routes_and_their_denials(team_db, monkeypatch):
    import json

    import httpx
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    from apps.api.routers import team_coordination
    from tests.test_mcp_http import _free_port, _serve
    from tests.test_team_transport import membership_driven, personal_app

    factory, a, b = team_db
    membership_driven(monkeypatch)
    async with factory() as session, session.begin():
        task_id = await make_parent(session, a, key="mcp-parent-01")
        run_id = await make_run(session, a, task_id, "mcp-run-0001", head=B)
    app = personal_app(factory, monkeypatch)
    app.include_router(team_coordination.router)

    def body(result):
        assert not result.is_error, result.content
        return json.loads(result.content[0].text)

    with _serve(app, _free_port()) as url:
        async def session_for(key):
            http_client = httpx.AsyncClient(headers={"Authorization": f"Bearer {key}"}, timeout=30)
            return http_client, Client(streamable_http_client(f"{url}/mcp", http_client=http_client))

        reader_http, reader = await session_for("fixture-1")
        async with reader_http, reader:
            names = {tool.name for tool in (await reader.list_tools()).tools}
            assert set(COORDINATION_WRITES) <= names
            denied = await reader.call_tool("team_claim_lease", {"project_id": str(a), "repository_id": 1,
                                                                 "resource": "src/api"})
            assert denied.is_error and "403" in denied.content[0].text

        http_client, client = await session_for("fixture-2")
        async with http_client, client:
            claim = {"project_id": str(a), "repository_id": 1, "resource": "src/api", "task_id": str(task_id)}
            lease = body(await client.call_tool("team_claim_lease", claim))["lease"]
            assert lease["holder_id"] == 2 and lease["fencing_token"] == 1
            assert body(await client.call_tool("team_claim_lease", claim))["lease"]["id"] == lease["id"], "a retry"
            assert (await client.call_tool("team_claim_lease", {**claim, "project_id": str(b)})).is_error
            renewed = body(await client.call_tool("team_renew_lease", {"project_id": str(a), "lease_id": lease["id"],
                                                                       "fencing_token": 1, "ttl_seconds": 120}))
            assert renewed["fencing_token"] == 1
            assert (await client.call_tool("team_release_lease", {"project_id": str(a), "lease_id": lease["id"],
                                                                  "fencing_token": 9})).is_error, "wrong token"
            released = body(await client.call_tool("team_release_lease", {"project_id": str(a),
                                                                          "lease_id": lease["id"], "fencing_token": 1}))
            assert released["state"] == "released"

            plan = body(await client.call_tool("team_declare_scope", {"project_id": str(a), "task_id": str(task_id),
                                                                      "scope_paths": ["src/api"],
                                                                      "expected_version": 0}))
            assert plan["plan"]["scope_paths"] == ["src/api"]
            stale = await client.call_tool("team_declare_scope", {"project_id": str(a), "task_id": str(task_id),
                                                                  "scope_paths": ["docs"], "expected_version": 0})
            assert stale.is_error and "409" in stale.content[0].text

            entry = body(await client.call_tool("team_enqueue_integration", {"project_id": str(a),
                                                                             "run_id": str(run_id)}))
            assert entry["status"] == "queued" and entry["head_sha"] == B
            again = body(await client.call_tool("team_enqueue_integration", {"project_id": str(a),
                                                                             "run_id": str(run_id)}))
            assert again["id"] == entry["id"], "a run is queued once"
