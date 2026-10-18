"""Team coordination (M3): subtask DAG + coordinator, scope overlaps, instruction versions, critical leases and the
current-base integration queue. Runs on SQLite, and on the shared PostgreSQL database when TEST_TEAM_POSTGRES_URL
is set (the ``team_db`` fixture from test_team_foundations)."""
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select, update

from brain.auth.principals import ResolvedPrincipal
from brain.database.harness_models import AgentTask, AgentTaskEvent
from brain.database.models import ApiCredential, Decision, Principal, Rule
from brain.team import combined_patch, coordination
from brain.team.activity import ActivityQuery, ActivityScope, list_activity
from brain.team.authorization import TeamConflict, TeamDenied
from brain.team.models import (AgentRun, TeamActivityEvent, TeamIntegrationEntry, TeamResourceLease,
                               TeamRunResolution)
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
        report = await coordination.list_overlaps(session, caller(1), a)
        listed = report["overlaps"]
        assert len(listed) == 2
        # /synthetic/A is not a checkout: no import graph, so only paths were compared, and the report says so.
        assert [(g["repository_id"], g["status"]) for g in report["dependency_graphs"]] == [(1, "unavailable")]
        assert result["dependency_graph"]["status"] == "unavailable"

        scope = ActivityScope(administrator=False, repository_ids=frozenset({1}), project_ids=frozenset({a}))
        feed = await list_activity(session, scope, ActivityQuery(kinds=frozenset({"coordination"})))
        assert [event["kind"] for event in feed["events"]] == ["coordination.overlap"] * 2
        hidden = ActivityScope(administrator=False, repository_ids=frozenset({2}), project_ids=frozenset())
        assert (await list_activity(session, hidden, ActivityQuery(kinds=frozenset({"coordination"}))))["events"] == []

        # A finished task stops counting.
        await set_status(session, other, "completed")
        assert (await coordination.list_overlaps(session, caller(1), a))["overlaps"] == [
            item for item in listed if item["kind"] == "task" and str(other) not in (item["task_id"], item["other_task_id"])]


# --------------------------------------------------------------------------- dependency overlap (import graph)


def fake_graph(edges, status="ok", reason=None, calls=None):
    """A dependency provider over a fixed, undirected import graph (``edges``: file -> files it imports)."""
    links: dict[str, set[str]] = {}
    for source, targets in edges.items():
        for target in targets:
            links.setdefault(source, set()).add(target)
            links.setdefault(target, set()).add(source)

    async def provider(session, repository_id, paths):
        if calls is not None:
            calls.append((repository_id, list(paths)))
        if status != "ok":
            return {"status": status, "reason": reason, "edges": {}, "truncated": False}
        return {"status": "ok", "reason": None, "edges": {p: sorted(links.get(p, ())) for p in paths},
                "truncated": False}
    return provider


def test_dependency_pairs_are_import_edges_between_scopes_in_either_direction():
    edges = {"src/api/routes.py": ["src/lib/db.py"], "src/lib/db.py": ["src/api/routes.py"]}
    assert coordination.dependency_pairs(["src/api/routes.py"], ["src/lib"], edges) == [
        ["src/api/routes.py", "src/lib/db.py"]]
    # Their file is imported from inside my directory scope: the edge is reported from my side.
    assert coordination.dependency_pairs(["src/api"], ["src/lib/db.py"], edges) == [
        ["src/api/routes.py", "src/lib/db.py"]]
    assert coordination.dependency_pairs(["docs"], ["src/lib/db.py"], edges) == []
    many = {"a.py": [f"lib/m{i:03}.py" for i in range(80)]}
    pairs = coordination.dependency_pairs(["a.py"], ["lib"], many)
    assert len(pairs) == coordination.MAX_OVERLAP_PAIRS and pairs == sorted(pairs)


@pytest.mark.asyncio
async def test_dependency_overlap_warns_without_blocking_and_reaches_the_feed(team_db, monkeypatch):
    factory, a, _ = team_db
    calls: list = []
    monkeypatch.setattr(coordination, "dependency_provider",
                        fake_graph({"src/api/routes.py": ["src/lib/db.py"]}, calls=calls))
    async with factory() as session, session.begin():
        lib = await make_parent(session, a, "dep-lib-0001")
        first = await coordination.update_plan(session, caller(2), a, lib,
                                               {"scope_paths": ["src/lib/db.py"], "expected_version": 0})
        assert first["warnings"] == [] and first["dependency_graph"]["status"] == "skipped", "no one else yet"
        assert calls == []
        api = await make_parent(session, a, "dep-api-0001")
        result = await coordination.update_plan(session, caller(3), a, api,
                                                {"scope_paths": ["src/api/routes.py", "src/api"],
                                                 "expected_version": 0})
        assert result["plan"]["scope_paths"] == ["src/api", "src/api/routes.py"], "the write went through"
        assert result["warnings"] == [{
            "kind": "dependency", "repository_id": 1, "task_id": str(api), "other_task_id": str(lib),
            "other_title": "synthetic", "relation": "imports", "paths": [["src/api/routes.py", "src/lib/db.py"]]}]
        assert result["dependency_graph"] == {"repository_id": 1, "status": "ok", "reason": None, "truncated": False}
        # The caller's files are looked up first, then the other tasks' files; directories are not looked up.
        assert calls[-1] == (1, ["src/api/routes.py", "src/lib/db.py"])

        again = await coordination.update_plan(session, caller(3), a, api,
                                               {"scope_paths": ["src/api", "src/api/routes.py"], "expected_version": 1})
        assert again["warnings"] == result["warnings"], "deterministic"
        rows = (await session.execute(select(TeamActivityEvent).where(
            TeamActivityEvent.kind == "coordination.overlap"))).scalars().all()
        assert len(rows) == 1, "recorded once"
        assert rows[0].details["kind"] == "dependency" and rows[0].summary == "Dependency overlap with task synthetic"

        reverse = await coordination.update_plan(session, caller(2), a, lib,
                                                  {"scope_paths": ["src/lib/db.py"], "expected_version": 1})
        assert reverse["warnings"][0]["other_task_id"] == str(api)
        rows = (await session.execute(select(TeamActivityEvent).where(
            TeamActivityEvent.kind == "coordination.overlap"))).scalars().all()
        assert len(rows) == 1, "the reverse task update has the same canonical fingerprint"

        report = await coordination.list_overlaps(session, caller(1), a)
        [item] = report["overlaps"]
        assert item["kind"] == "dependency" and {item["task_id"], item["other_task_id"]} == {str(lib), str(api)}
        edge = ["src/api/routes.py", "src/lib/db.py"]
        assert item["paths"] == [edge if item["task_id"] == str(api) else edge[::-1]], "oriented from task_id"
        assert report["dependency_graphs"] == [{"repository_id": 1, "status": "ok", "reason": None,
                                                "truncated": False}]

        # A shared path says more than an import: the pair gets the path warning only.
        shared = await make_parent(session, a, "dep-shared-01")
        both = await coordination.update_plan(session, caller(2), a, shared,
                                              {"scope_paths": ["src/lib/db.py", "src/api/routes.py"],
                                               "expected_version": 0})
        assert sorted(w["kind"] for w in both["warnings"]) == ["task", "task"]


@pytest.mark.asyncio
async def test_dependency_overlap_degrades_to_paths_when_the_graph_is_stale_or_missing(team_db, monkeypatch):
    factory, a, _ = team_db
    async with factory() as session, session.begin():
        lib = await make_parent(session, a, "deg-lib-0001")
        await coordination.update_plan(session, caller(2), a, lib, {"scope_paths": ["src/lib/db.py"],
                                                                     "expected_version": 0})
        api = await make_parent(session, a, "deg-api-0001")
        version = 0
        for status, reason in (("stale", "The repository index is behind; re-index to compare imports"),
                               ("unavailable", "The repository index is unindexed")):
            monkeypatch.setattr(coordination, "dependency_provider",
                                fake_graph({"src/api/routes.py": ["src/lib/db.py"]}, status=status, reason=reason))
            result = await coordination.update_plan(session, caller(3), a, api, {
                "scope_paths": ["src/api/routes.py"], "expected_version": version})
            version += 1
            assert result["warnings"] == [], "no dependency warning without a trusted graph"
            assert result["dependency_graph"] == {"repository_id": 1, "status": status, "reason": reason,
                                                  "truncated": False}
            report = await coordination.list_overlaps(session, caller(1), a)
            assert report["overlaps"] == [] and report["dependency_graphs"][0]["status"] == status

        # Past MAX_DEPENDENCY_PATHS files the lookup is bounded and the response says it was partial.
        calls: list = []
        monkeypatch.setattr(coordination, "dependency_provider", fake_graph({}, calls=calls))
        wide = [f"src/w/f{i:02}.py" for i in range(40)]
        result = await coordination.update_plan(session, caller(3), a, api, {"scope_paths": wide,
                                                                             "expected_version": version})
        assert len(calls[-1][1]) == coordination.MAX_DEPENDENCY_PATHS and calls[-1][1] == sorted(wide)[:32]
        assert result["dependency_graph"]["truncated"] is True


@pytest.mark.asyncio
async def test_default_dependency_provider_reads_imports_only_from_a_current_index(team_db, monkeypatch, tmp_path):
    import asyncio

    import brain.graph.graph_client as graph_client
    import brain.memory.repo_freshness as repo_freshness
    from brain.database.models import Repository

    factory, _, _ = team_db
    freshness = {"status": "current"}

    async def assess(repo):
        return dict(freshness)

    class FakeGraph:
        delay = 0.0

        def __init__(self, repository_id):
            assert repository_id == 1

        async def get_import_neighbors(self, path, limit):
            await asyncio.sleep(self.delay)
            assert limit == coordination.MAX_DEPENDENCY_NEIGHBORS + 1
            if path == "boom.py":
                raise RuntimeError("neo4j down")
            file = lambda name: {"labels": ["File"], "properties": {"name": name}}  # noqa: E731
            return [{"relationship": {"type": "IMPORTS"}, "target": file("src/lib/db.py")},
                    {"relationship": {"type": "TESTED_BY"}, "target": file("tests/test_routes.py")},
                    {"relationship": {"type": "IMPORTS"}, "target": {"labels": ["Module"], "properties": {"name": "x"}}},
                    {"relationship": {"type": "IMPORTS"}, "target": file(path)}]

    monkeypatch.setattr(repo_freshness, "assess_repository_freshness", assess)
    monkeypatch.setattr(graph_client, "GraphClient", FakeGraph)
    async with factory() as session, session.begin():
        missing = await coordination.graph_dependencies(session, 1, ["src/api/routes.py"])
        assert missing["status"] == "unavailable" and "not available" in missing["reason"], "/synthetic/A"
        await session.execute(update(Repository).where(Repository.id == 1).values(path=str(tmp_path)))

        ok = await coordination.graph_dependencies(session, 1, ["src/api/routes.py"])
        assert ok == {"status": "ok", "reason": None, "edges": {"src/api/routes.py": ["src/lib/db.py"]},
                      "truncated": False}
        failed = await coordination.graph_dependencies(session, 1, ["boom.py"])
        assert failed["status"] == "unavailable" and "RuntimeError" in failed["reason"]
        monkeypatch.setattr(coordination, "DEPENDENCY_TIMEOUT_SECONDS", 0.05)
        FakeGraph.delay = 1.0
        slow = await coordination.graph_dependencies(session, 1, ["src/api/routes.py"])
        assert slow["status"] == "unavailable" and "timed out" in slow["reason"]
        FakeGraph.delay = 0.0
        for status, expected in (("behind", "stale"), ("stale", "stale"), ("unindexed", "unavailable"),
                                 ("source_missing", "unavailable")):
            freshness["status"] = status
            result = await coordination.graph_dependencies(session, 1, ["src/api/routes.py"])
            assert (result["status"], result["edges"]) == (expected, {}), status
            assert status in result["reason"]


# --------------------------------------------------------------------------- combined patch (temp git repository)

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def git(cwd, *args) -> str:
    env = {"PATH": os.environ.get("PATH", ""), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.invalid", "HOME": str(cwd)}
    for name in ("SYSTEMROOT", "WINDIR", "PATHEXT"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, check=True).stdout.decode().strip()


@dataclass
class GitProject:
    src: Path
    root: Path
    base: str

    def run_copy(self, name: str, changes: dict[str, str]) -> tuple[Path, str]:
        """A run's working copy as the executor leaves it: a clone under the execution root with one commit."""
        path = self.root / name / "repo"
        path.parent.mkdir(parents=True)
        git(path.parent, "clone", "--quiet", str(self.src), str(path))
        for file, text in changes.items():
            (path / file).write_bytes(text.encode())
        git(path, "commit", "--quiet", "-am", name)
        return path, git(path, "rev-parse", "HEAD")

    def snapshot(self) -> tuple[str, str, str]:
        return (git(self.src, "for-each-ref"), git(self.src, "status", "--porcelain"),
                git(self.src, "rev-parse", "HEAD"))


def git_project(tmp_path) -> GitProject:
    src = tmp_path / "src"
    src.mkdir()
    git(src, "init", "--quiet")
    for file, text in {"f.txt": "a\nb\nc\n", "g.txt": "x\n", "h.txt": "k\n", "blob.bin": "\0base"}.items():
        (src / file).write_bytes(text.encode())
    git(src, "add", ".")
    git(src, "commit", "--quiet", "-m", "base")
    return GitProject(src=src, root=tmp_path / "runs", base=git(src, "rev-parse", "HEAD"))


def test_conflict_paths_and_object_stores(tmp_path):
    stderr = ("error: patch failed: f.txt:1\nerror: f.txt: patch does not apply\n"
              "error: new.txt: already exists in index\nerror: gone.txt: does not exist in index\nwarning: x\n")
    assert combined_patch.conflict_paths(stderr) == ["f.txt", "new.txt", "gone.txt"]
    assert combined_patch.object_store(None) is None
    assert combined_patch.object_store(tmp_path / "missing") is None
    if shutil.which("git") is None:
        return
    project = git_project(tmp_path)
    objects = (project.src / ".git" / "objects").resolve()
    assert combined_patch.object_store(project.src) == objects
    git(project.src, "worktree", "add", "--quiet", str(tmp_path / "linked"))
    assert combined_patch.object_store(tmp_path / "linked") == objects, "a linked worktree reads the common objects"
    git(tmp_path, "clone", "--quiet", "--bare", str(project.src), str(tmp_path / "bare.git"))
    assert combined_patch.object_store(tmp_path / "bare.git") == (tmp_path / "bare.git" / "objects").resolve()


@needs_git
def test_combined_patch_applies_integrated_patches_first_and_names_conflicts(tmp_path, monkeypatch):
    project = git_project(tmp_path)
    one, one_head = project.run_copy("one", {"f.txt": "a\nB1\nc\n"})
    two, two_head = project.run_copy("two", {"g.txt": "x2\n"})
    three, three_head = project.run_copy("three", {"f.txt": "a\nB3\nc\n", "h.txt": "k3\n"})
    before = project.snapshot()
    copies_before = [git(path, "status", "--porcelain") + git(path, "rev-parse", "HEAD") for path in (one, two, three)]
    sources = [project.src, one, two, three]
    first, second, third = (combined_patch.Patch("1", one_head), combined_patch.Patch("2", two_head),
                            combined_patch.Patch("3", three_head))

    clean = combined_patch.combine(project.base, [first], second, sources)
    assert clean["status"] == "clean" and clean["reason"] is None and clean["applied"] == ["1", "2"]
    assert clean["files"] == [{"path": "f.txt", "added": 1, "deleted": 1}, {"path": "g.txt", "added": 1, "deleted": 1}]
    assert (clean["files_total"], clean["insertions"], clean["deletions"], clean["truncated"]) == (2, 2, 2, False)
    assert "+B1" in clean["patch"] and "+x2" in clean["patch"] and len(clean["tree_sha"]) == 40
    assert combined_patch.summary(clean).keys() == clean.keys() - {"patch", "files"}

    conflict = combined_patch.combine(project.base, [first, second], third, sources)
    assert (conflict["status"], conflict["conflicts"], conflict["applied"]) == ("conflict", ["f.txt"], ["1", "2"])
    assert conflict["patch"] is None and conflict["tree_sha"] is None

    broken = combined_patch.combine(project.base, [first, third], second, sources)
    assert broken["status"] == "unavailable" and "entry 3" in broken["reason"], "integrated patches must combine"
    missing = combined_patch.combine(project.base, [first], second, [project.src, one])
    assert missing["status"] == "unavailable" and two_head[:12] in missing["reason"]
    assert combined_patch.combine(project.base, [], second, [tmp_path / "nowhere"])["status"] == "unavailable"
    assert combined_patch.combine("main", [], second, sources)["reason"] == "A revision is not a full commit SHA"

    monkeypatch.setattr(combined_patch, "MAX_PATCH_BYTES", 40)
    short = combined_patch.combine(project.base, [first], second, sources)
    assert short["status"] == "clean" and short["truncated"] is True and len(short["patch"].encode()) <= 40

    assert project.snapshot() == before, "the source checkout is not touched"
    assert [git(path, "status", "--porcelain") + git(path, "rev-parse", "HEAD")
            for path in (one, two, three)] == copies_before, "nor are the runs' working copies"


@needs_git
def test_combined_patch_includes_binary_data_and_bounds_git_output(tmp_path, monkeypatch):
    project = git_project(tmp_path)
    run, head = project.run_copy("binary", {"blob.bin": "\0changed-binary-content"})
    patch = combined_patch.Patch("binary", head)
    result = combined_patch.combine(project.base, [], patch, [project.src, run])
    assert result["status"] == "clean" and "GIT binary patch" in result["patch"]
    monkeypatch.setattr(combined_patch, "MAX_DIFF_BYTES", 40)
    bounded = combined_patch.combine(project.base, [], patch, [project.src, run])
    assert bounded["status"] == "unavailable" and bounded["reason"] == "A patch is too large to check"


@needs_git
@pytest.mark.asyncio
async def test_integration_combines_patches_on_the_same_base_or_rejects_with_the_conflicts(team_db, tmp_path,
                                                                                         monkeypatch):
    from brain.config.settings import settings
    from brain.database.models import Repository

    factory, a, _ = team_db
    project = git_project(tmp_path)
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_ROOT", str(project.root))
    copies = [project.run_copy("one", {"f.txt": "a\nB1\nc\n"}), project.run_copy("two", {"g.txt": "x2\n"}),
              project.run_copy("three", {"f.txt": "a\nB3\nc\n"}), project.run_copy("five", {"h.txt": "k5\n"})]
    before = project.snapshot()

    async def queued(session, index, head, base=project.base, worktree=None):
        task = await make_parent(session, a, f"cp-task-{index:04}")
        run = await make_run(session, a, task, f"cp-run-{index:04}", head=head)
        await session.execute(update(AgentRun).where(AgentRun.id == run).values(
            base_sha=base, worktree_path=str(worktree) if worktree else None))
        return await coordination.enqueue(session, caller(2), a, {"run_id": str(run)})

    async with factory() as session, session.begin():
        await session.execute(update(Repository).where(Repository.id == 1).values(path=str(project.src)))
        e1, e2, e3 = [await queued(session, i, head, worktree=path) for i, (path, head) in enumerate(copies[:3])]

        first = await coordination.integrate(session, caller(3), a, e1["id"], {"expected_version": 1,
                                                                              "current_base_sha": project.base})
        assert first["status"] == "integrated" and first["combined_patch"]["status"] == "clean"
        assert first["combined_patch"]["applied"] == [str(e1["id"])], "on its own base: the patch alone"

        # Built on the same base as the entry just integrated: applied after it instead of a rebase.
        second = await coordination.integrate(session, caller(3), a, e2["id"], {"expected_version": 1})
        assert second["status"] == "integrated" and second["checked_base_sha"] == copies[0][1]
        combined = second["combined_patch"]
        assert combined["applied"] == [str(e1["id"]), str(e2["id"])]
        assert [item["path"] for item in combined["files"]] == ["f.txt", "g.txt"] and "+x2" in combined["patch"]
        event = (await session.execute(select(TeamActivityEvent).where(
            TeamActivityEvent.kind == "coordination.integrated").order_by(TeamActivityEvent.id.desc())
            .limit(1))).scalar_one()
        assert event.details["combined_patch"]["files_total"] == 2 and "patch" not in event.details["combined_patch"]

        # The third run edits the line the first one changed: rejected with the path, the task stays open.
        third = await coordination.integrate(session, caller(3), a, e3["id"], {"expected_version": 1})
        assert third["status"] == "rejected" and third["decided_by"] == 3
        assert third["reason"] == f"Patch conflicts with integrated tasks on base {project.base[:12]}: f.txt"
        assert third["combined_patch"]["conflicts"] == ["f.txt"]
        task = await session.get(AgentTask, uuid.UUID(e3["task_id"]))
        assert task.status != "completed"

        # No commit holds the combined result: a run built on the second head is sent back, and the first head
        # (which the second entry was checked against) is no longer a valid target.
        e4 = await queued(session, 4, "f" * 40, base=copies[1][1])
        sent_back = await coordination.integrate(session, caller(3), a, e4["id"], {"expected_version": 1})
        assert sent_back["status"] == "needs_rebase" and "combined integration" in sent_back["reason"]
        with pytest.raises(TeamConflict, match="predates"):
            await coordination.integrate(session, caller(3), a, e4["id"], {"expected_version": 2,
                                                                          "current_base_sha": copies[0][1]})

        # Working copies outside the execution root are not read: the check cannot run, so it is a rebase.
        e5 = await queued(session, 5, copies[3][1], worktree=copies[3][0])
        monkeypatch.setattr(settings, "BRAIN_EXECUTION_ROOT", str(tmp_path / "elsewhere"))
        fallback = await coordination.integrate(session, caller(3), a, e5["id"], {"expected_version": 1})
        assert fallback["status"] == "needs_rebase" and "combined patch check unavailable" in fallback["reason"]
    assert project.snapshot() == before, "nothing was merged, committed or checked out in the source repository"


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
async def test_admin_force_release_bumps_the_token_and_reaches_the_activity_feed(team_db):
    factory, a, _ = team_db
    async with factory() as session, session.begin():
        task_id = await make_parent(session, a, who=3)
        held = (await coordination.acquire_lease(session, caller(3), a, {
            "repository_id": 1, "resource": "src/api", "task_id": str(task_id)}))["lease"]
        for who in (1, 2, 3):  # Reader, Contributor and even the holder (a Reviewer) are not administrators
            with pytest.raises(TeamDenied, match="Administration permission required"):
                await coordination.force_release_lease(session, caller(who), a, held["id"], {"reason": "stuck"})
        for reason in (None, "", "   "):
            with pytest.raises(TeamConflict, match="reason is required"):
                await coordination.force_release_lease(session, caller(4), a, held["id"], {"reason": reason})
        with pytest.raises(TeamConflict, match="fencing token changed"):
            await coordination.force_release_lease(session, caller(4), a, held["id"], {
                "reason": "stuck", "fencing_token": 7})
        with pytest.raises(TeamNotFound):
            await coordination.force_release_lease(session, caller(4), a, 9999, {"reason": "stuck"})

        released = await coordination.force_release_lease(session, caller(4), a, held["id"], {
            "reason": "  holder   is on leave ", "fencing_token": 1})
        assert released["state"] == "released" and released["fencing_token"] == 2
        assert released["released_reason"] == "force released: holder is on leave"
        # The previous holder can neither renew nor release with the token it holds.
        for action in (coordination.renew_lease, coordination.release_lease):
            with pytest.raises(TeamConflict, match="fencing token"):
                await action(session, caller(3), a, held["id"], {"fencing_token": 1})
        with pytest.raises(TeamConflict, match="already released"):
            await coordination.force_release_lease(session, caller(4), a, held["id"], {"reason": "again"})
        # The resource is free at once, past the burned token.
        taken = (await coordination.acquire_lease(session, caller(2), a, {"repository_id": 1, "resource": "src/api"}))
        assert taken["lease"]["fencing_token"] == 3 and taken["lease"]["holder_id"] == 2

        rows = (await session.execute(select(TeamActivityEvent).where(
            TeamActivityEvent.kind == "coordination.lease_force_released"))).scalars().all()
        assert len(rows) == 1
        event = rows[0]
        assert event.actor_principal_id == 4 and event.subject_principal_id == 3 and event.repository_id == 1
        assert event.details["reason"] == "holder is on leave" and event.details["task_id"] == str(task_id)
        assert (event.details["previous_fencing_token"], event.details["fencing_token"]) == (1, 2)
        scope = ActivityScope(administrator=False, repository_ids=frozenset({1}), project_ids=frozenset({a}))
        feed = await list_activity(session, scope, ActivityQuery(kinds=frozenset({"coordination"})))
        assert "coordination.lease_force_released" in [item["kind"] for item in feed["events"]]


@pytest.mark.asyncio
async def test_operator_force_release_is_recorded_as_the_server_operator(team_db):
    """``brain team coord force-release`` runs as the trusted operator: no principal, a labelled actor."""
    factory, a, _ = team_db
    async with factory() as session, session.begin():
        held = (await coordination.acquire_lease(session, caller(2), a, {
            "repository_id": 1, "resource": "resource:deploy"}))["lease"]
        with pytest.raises(TeamConflict, match="reason is required"):
            await coordination.operator_force_release_lease(session, a, held["id"], {"reason": " "},
                                                            actor_label="operator:cli")
        with pytest.raises(TeamNotFound):
            await coordination.operator_force_release_lease(session, uuid.uuid4(), held["id"], {"reason": "x"},
                                                            actor_label="operator:cli")
        released = await coordination.operator_force_release_lease(session, a, held["id"], {"reason": "deploy hung"},
                                                                   actor_label="operator:cli")
        assert released["state"] == "released" and released["fencing_token"] == 2
        [event] = (await session.execute(select(TeamActivityEvent).where(
            TeamActivityEvent.kind == "coordination.lease_force_released"))).scalars().all()
        assert event.actor_principal_id is None and event.actor_label == "operator:cli"
        assert event.subject_principal_id == 2 and event.details["reason"] == "deploy hung"
        assert "server operator" in event.summary


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
        queue = await coordination.list_queue(session, caller(1), a)
        assert [item["id"] for item in queue] == [e1["id"], e2["id"]]
        assert (queue[0]["initiator_id"], queue[0]["requested_by"]) == (2, 2)

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
        assert "combined patch check unavailable" in rebase["reason"], "no git objects for synthetic revisions"
        assert rebase["combined_patch"]["status"] == "unavailable"
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


@pytest.mark.asyncio
async def test_integration_outcomes_settle_the_task(team_db):
    factory, a, _ = team_db
    async with factory() as session, session.begin():
        parent = await make_parent(session, a)
        s1 = uuid.UUID((await coordination.create_subtask(session, caller(2), a, parent, sub("one")))["id"])
        s2 = uuid.UUID((await coordination.create_subtask(session, caller(2), a, parent, sub("two", deps=[s1])))["id"])
        before = (await session.execute(select(AgentTask).where(AgentTask.id == s1))).scalar_one()
        status, version = before.status, before.version
        # A rejected entry leaves the task open (another run may still land) and is recorded on its ledger.
        r0 = await make_run(session, a, s1, "settle-run-00", head=B)
        e0 = await coordination.enqueue(session, caller(2), a, {"run_id": str(r0)})
        await coordination.reject(session, caller(3), a, e0["id"], {"reason": "not this one", "expected_version": 1})
        task = (await session.execute(select(AgentTask).where(AgentTask.id == s1)
                                      .execution_options(populate_existing=True))).scalar_one()
        assert task.status == status and task.version == version
        with pytest.raises(TeamConflict, match="Dependencies are not done"):
            await request_run(session, caller(2), a, s2, run_payload("settle-dep-01"))

        r1 = await make_run(session, a, s1, "settle-run-01", head=C)
        e1 = await coordination.enqueue(session, caller(2), a, {"run_id": str(r1)})
        assert (await coordination.integrate(session, caller(3), a, e1["id"], {
            "expected_version": 1, "current_base_sha": A}))["status"] == "integrated"
        task = (await session.execute(select(AgentTask).where(AgentTask.id == s1)
                                      .execution_options(populate_existing=True))).scalar_one()
        assert task.status == "completed" and task.version == version + 1
        events = {row.event_type: row for row in (await session.execute(select(AgentTaskEvent).where(
            AgentTaskEvent.task_id == s1, AgentTaskEvent.idempotency_key.like("integration:%")))).scalars().all()}
        assert set(events) == {"integration.rejected", "status_change"}
        assert events["integration.rejected"].payload_json["reason"] == "not this one"
        change = events["status_change"]
        assert change.payload_json["to"] == "completed" and change.payload_json["entry_id"] == e1["id"]
        assert (change.expected_task_version, change.task_version) == (version, version + 1)
        assert change.classification == "decision"

        # "Done" now follows the task status, not only the queue row.
        await session.execute(update(TeamIntegrationEntry).values(status="rejected"))
        assert str(s1) in await coordination._done(session, [s1])
        await request_run(session, caller(2), a, s2, run_payload("settle-dep-02"))
        states = {item["id"]: item["state"] for item in (await coordination.plan_view(session, a, parent))["subtasks"]}
        assert states[str(s1)] == "done"


@pytest.mark.asyncio
async def test_changed_files_overlap_scopes_and_leases_once_per_pair(team_db):
    factory, a, _ = team_db
    async with factory() as session, session.begin():
        mine = await make_parent(session, a)
        other = await make_parent(session, a, "parent-0002")
        await coordination.update_plan(session, caller(2), a, other, {"scope_paths": ["docs/*"], "expected_version": 0})
        lease = (await coordination.acquire_lease(session, caller(3), a, {
            "repository_id": 1, "resource": "src/core"}))["lease"]
        own_lease = await coordination.acquire_lease(session, caller(2), a, {
            "repository_id": 1, "resource": "src/mine", "task_id": str(mine)})
        assert own_lease["warnings"] == []
        changed = ["docs/guide.md", "src/core/db.py", "src/mine/x.py", "README.md", "app/[id]/page.tsx",
                   " leading.txt", "dir\\name.txt", "../escape", "/abs/path"]
        warnings = await coordination.record_change_overlaps(session, mine, changed, actor_id=2)
        assert sorted((w["kind"], w["source"]) for w in warnings) == [("lease", "changed_paths"),
                                                                      ("task", "changed_paths")]
        by_kind = {w["kind"]: w for w in warnings}
        assert by_kind["task"]["other_task_id"] == str(other)
        assert by_kind["task"]["paths"] == [["docs/guide.md", "docs/*"]]
        assert by_kind["lease"]["lease_id"] == lease["id"]
        assert by_kind["lease"]["paths"] == [["src/core/db.py", "src/core"]]
        # Recorded once per pair: a retry (or a second run with the same changes) adds nothing.
        assert len(await coordination.record_change_overlaps(session, mine, changed, actor_id=2)) == 2
        rows = (await session.execute(select(TeamActivityEvent).where(
            TeamActivityEvent.kind == "coordination.overlap"))).scalars().all()
        assert len(rows) == 2 and all(row.details["source"] == "changed_paths" for row in rows)
        assert sorted(row.summary for row in rows) == ["Changed files overlap leased resource src/core",
                                                       "Changed files overlap the scope of task synthetic"]
        assert coordination.changed_scope(changed) == [" leading.txt", "README.md", "app/[id]/page.tsx",
                                                       "dir\\name.txt", "docs/guide.md", "src/core/db.py",
                                                       "src/mine/x.py"]
        assert len(coordination.changed_scope([f"files/{index:04d}.txt" for index in range(600)])) == 600
        # A later run with different files under the same broad scope/lease does not add activity rows.
        await coordination.record_change_overlaps(session, mine, ["docs/other.md", "src/core/new.py"], actor_id=2)
        rows = (await session.execute(select(TeamActivityEvent).where(
            TeamActivityEvent.kind == "coordination.overlap"))).scalars().all()
        assert len(rows) == 2
        assert await coordination.record_change_overlaps(session, mine, None, actor_id=2) == []
        assert await coordination.record_change_overlaps(session, mine, ["unrelated.txt"], actor_id=2) == []
        # Finished tasks and released leases stop counting.
        await set_status(session, other, "completed")
        await coordination.force_release_lease(session, caller(4), a, lease["id"], {"reason": "done"})
        assert await coordination.record_change_overlaps(session, mine, changed, actor_id=2) == []


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
        assert overlaps.json()["dependency_graphs"][0]["status"] == "unavailable", "no checkout, paths only"

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
        assert integrated.json()["combined_patch"]["status"] == "unavailable", "no checkout behind /synthetic/A"
        queue = await client.get(f"{base}/integration", headers=READER, params={"include_done": "true"})
        assert [item["status"] for item in queue.json()["entries"]] == ["integrated"]
        assert (await client.post(f"{base}/integration/{entry['id']}/reject", headers=reviewer,
                                  json={"reason": "late", "expected_version": 2})).status_code == 409


@pytest.mark.asyncio
async def test_http_force_release_is_for_team_administrators_only(team_db, monkeypatch):
    from apps.api.routers import team_coordination
    from tests.test_team_transport import CONTRIBUTOR, READER, client_for, membership_driven, personal_app

    factory, a, b = team_db
    membership_driven(monkeypatch)
    app = personal_app(factory, monkeypatch)
    app.include_router(team_coordination.router)
    reviewer, admin = {"X-API-Key": "fixture-3"}, {"X-API-Key": "fixture-4"}
    base = f"/team/projects/{a}"
    async with client_for(app) as client:
        lease = (await client.post(f"{base}/leases", headers=reviewer,
                                   json={"repository_id": 1, "resource": "src/api"})).json()["lease"]
        url = f"{base}/leases/{lease['id']}/force-release"
        # Reader, Contributor and the holder itself (a Reviewer) are members, not administrators.
        for headers in (READER, CONTRIBUTOR, reviewer):
            denied = await client.post(url, headers=headers, json={"reason": "stuck"})
            assert denied.status_code == 403, denied.text
        assert (await client.post(url, headers={"X-API-Key": "nope"}, json={"reason": "stuck"})).status_code == 401
        for payload in ({}, {"reason": ""}, {"reason": "   "}, {"reason": "x" * 501},
                        {"reason": "stuck", "fencing_token": 0}, {"reason": "stuck", "extra": 1}):
            assert (await client.post(url, headers=admin, json=payload)).status_code == 422, payload
        assert (await client.post(f"{base}/leases/424242/force-release", headers=admin,
                                  json={"reason": "stuck"})).status_code == 404
        assert (await client.post(f"/team/projects/{b}/leases/{lease['id']}/force-release", headers=admin,
                                  json={"reason": "stuck"})).status_code == 404, "the lease belongs to another project"
        moved = await client.post(url, headers=admin, json={"reason": "stuck", "fencing_token": 5})
        assert moved.status_code == 409 and "fencing token changed" in moved.json()["detail"]
        released = await client.post(url, headers=admin, json={"reason": "holder is on leave", "fencing_token": 1})
        assert released.status_code == 200, released.text
        assert released.json()["state"] == "released" and released.json()["fencing_token"] == 2
        assert (await client.post(url, headers=admin, json={"reason": "again"})).status_code == 409
        assert (await client.post(f"{base}/leases/{lease['id']}/release", headers=reviewer,
                                  json={"fencing_token": 1})).status_code == 409, "the old token is burned"


COORDINATION_READS = ("team_task_plan", "team_coordination_overlaps", "team_list_leases", "team_integration_queue")
COORDINATION_WRITES = ("team_claim_lease", "team_renew_lease", "team_release_lease", "team_declare_scope",
                       "team_enqueue_integration", "team_force_release_lease")


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
        assert tools[name].annotations.idempotent_hint is (name != "team_renew_lease"), name


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

            held = body(await client.call_tool("team_claim_lease", claim))["lease"]
            force = {"project_id": str(a), "lease_id": held["id"], "reason": "stuck"}
            member = await client.call_tool("team_force_release_lease", force)
            assert member.is_error and "403" in member.content[0].text, "members cannot force-release"

        admin_http, admin = await session_for("fixture-4")
        async with admin_http, admin:
            forced = body(await admin.call_tool("team_force_release_lease", {**force, "fencing_token": held["fencing_token"]}))
            assert forced["state"] == "released" and forced["released_reason"] == "force released: stuck"
            again = await admin.call_tool("team_force_release_lease", force)
            assert again.is_error and "409" in again.content[0].text
