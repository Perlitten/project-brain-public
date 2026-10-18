"""The offline CLIs read one registry: the shared table in team state, the local file in solo state.

``brain.team.cli_registry`` decides for lab, freshness, experiments, portfolio,
control (and the workspace CLI): team state reads ``workspace_registry_entries``
(the table the API and the worker read), solo state the local file, and an
unreachable database falls back to the file with a warning, unless the
directory carries the team-state marker or ``BRAIN_TEAM_MODE`` is on, where the
CLI refuses with exit code 2 instead of silently diverging from the team.

The CLIs run synchronously (``asyncio.run`` per invocation), so these tests are
synchronous too and use a file-backed SQLite database with ``NullPool``.
"""
from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
from typing import Optional, Sequence
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from brain.config.settings import settings
from brain.database.models import Base
from brain.team import cli_registry
from brain.team import registry as shared
from brain.team.models import WorkspaceRegistryEntry, WorkspaceRegistryEvent
from brain.workspace.models import RepositoryRecord, TrustLevel, _generate_repository_id
from brain.workspace.registry_store import RegistryStore

REGISTRY_DIR = Path(".brain/workspace")


def _record(root: Path) -> RepositoryRecord:
    """A registered repository without capabilities: lab and experiments stop at the capability check."""
    return RepositoryRecord(
        repository_id=_generate_repository_id(str(root)), display_name=root.name, canonical_root=str(root),
        normalized_root_identity=str(root).lower(), repository_type="fixture", default_branch="main",
        current_revision="", trust_level=TrustLevel.FIXTURE.value, organization="", owner="",
    )


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A file registry with one repository and a team table with two others, in a fresh working directory."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "BRAIN_TEAM_MODE", False)
    roots = {name: (tmp_path / name).resolve() for name in ("file-only", "table-one", "table-two")}
    for root in roots.values():
        root.mkdir()
    records = {name: _record(root) for name, root in roots.items()}
    RegistryStore(REGISTRY_DIR).register(records["file-only"], actor="test", source="test")
    url = f"sqlite+aiosqlite:///{tmp_path}/registry.db"

    async def seed() -> None:
        engine = create_async_engine(url, poolclass=NullPool)
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync: Base.metadata.create_all(
                sync, tables=[WorkspaceRegistryEntry.__table__, WorkspaceRegistryEvent.__table__]))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            for name in ("table-one", "table-two"):
                await shared.insert_entry(session, records[name], actor="test", source="test")
        await engine.dispose()

    asyncio.run(seed())
    factory = async_sessionmaker(create_async_engine(url, poolclass=NullPool), expire_on_commit=False)
    monkeypatch.setattr("brain.database.session.async_session_factory", factory)
    return {name: record.repository_id for name, record in records.items()}


def team_state(monkeypatch, state: str = "team") -> None:
    monkeypatch.setattr("brain.workers.initiator.current_team_state", AsyncMock(return_value=state))


def database_down(monkeypatch) -> None:
    monkeypatch.setattr("brain.workers.initiator.current_team_state",
                        AsyncMock(side_effect=ConnectionRefusedError("synthetic")))


def run(capsys, module_name: str, argv: Sequence[str]) -> tuple[int, str, str]:
    module = importlib.import_module(module_name)
    code: Optional[int] = 0
    try:
        result = module.main(list(argv))
        if isinstance(result, int):
            code = result
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    captured = capsys.readouterr()
    return int(code or 0), captured.out, captured.err


# Per CLI group: (module, argv for one repository id) -> what proves the registry it read.


def lab(repository_id: str) -> tuple[str, list[str]]:
    return "brain.lab.cli", ["validate", "--repository", repository_id, "--patch-file", "missing.diff"]


def experiments(repository_id: str) -> tuple[str, list[str]]:
    return "brain.experiments.cli", ["create", "--repository", repository_id, "--option", "missing.json"]


def freshness(repository_id: str) -> tuple[str, list[str]]:
    return "brain.freshness.cli", ["status", "--repository", repository_id]


def found(code: int, err: str) -> bool:
    """The command resolved the repository: past "not registered" (lab/experiments then stop at capabilities)."""
    return "not registered" not in err and (code == 0 or "lacks capability" in err)


@pytest.mark.parametrize("command", [lab, experiments, freshness], ids=["lab", "experiments", "freshness"])
def test_team_state_reads_the_shared_table(world, monkeypatch, capsys, command):
    team_state(monkeypatch)
    code, _, err = run(capsys, *command(world["table-one"]))
    assert found(code, err), err
    code, _, err = run(capsys, *command(world["file-only"]))
    assert code == 1 and "not registered" in err
    assert cli_registry.marker_path(REGISTRY_DIR).is_file()


@pytest.mark.parametrize("command", [lab, experiments, freshness], ids=["lab", "experiments", "freshness"])
def test_solo_state_reads_the_local_file(world, monkeypatch, capsys, command):
    team_state(monkeypatch, "solo")
    code, _, err = run(capsys, *command(world["file-only"]))
    assert found(code, err), err
    code, _, err = run(capsys, *command(world["table-one"]))
    assert code == 1 and "not registered" in err


def _portfolio(capsys, ids: dict[str, str]) -> str:
    config = Path("portfolio.yaml")
    config.write_text(json.dumps({"version": 1, "portfolio": {"name": "synthetic"}, "repositories": {
        name: {"repository_id": repository_id} for name, repository_id in ids.items()}}), encoding="utf-8")
    code, out, err = run(capsys, "brain.portfolio.cli", ["create", "--config", str(config)])
    assert code == 0, err
    return json.loads(out[out.index("{"):])["portfolio_id"]


@pytest.mark.parametrize("state, missing", [("team", "file-only"), ("solo", "table-one")])
def test_portfolio_build_reads_the_registry_of_the_current_state(world, monkeypatch, capsys, state, missing):
    portfolio_id = _portfolio(capsys, {"file-only": world["file-only"], "table-one": world["table-one"]})
    team_state(monkeypatch, state)
    code, _, err = run(capsys, "brain.portfolio.cli", ["build", portfolio_id])
    assert code == 1
    reported = err.split("not registered: ", 1)[1].splitlines()[0]
    assert reported == world[missing]


@pytest.mark.parametrize("state, total", [("team", 2), ("solo", 1)])
def test_control_summary_counts_the_registry_of_the_current_state(world, monkeypatch, capsys, state, total):
    team_state(monkeypatch, state)
    code, out, err = run(capsys, "brain.control.cli", ["summary"])
    assert code == 0, err
    assert json.loads(out)["repositories"]["total"] == total


# The explicit offline fallback, and its refusal once the directory is known to be in team state.


def test_unreachable_database_falls_back_to_the_file_with_a_warning(world, monkeypatch, capsys):
    database_down(monkeypatch)
    code, _, err = run(capsys, *lab(world["file-only"]))
    assert "lacks capability" in err and "WARNING: cannot reach the team database" in err
    assert "solo/offline fallback" in err
    code, out, err = run(capsys, "brain.control.cli", ["summary"])
    assert code == 0 and json.loads(out)["repositories"]["total"] == 1


@pytest.mark.parametrize("group", ["lab", "experiments", "freshness", "portfolio", "control", "workspace"])
def test_unreachable_database_in_team_state_is_an_error(world, monkeypatch, capsys, group):
    portfolio_id = _portfolio(capsys, {"file-only": world["file-only"]})
    command = {
        "lab": lab(world["file-only"]), "experiments": experiments(world["file-only"]),
        "freshness": freshness(world["file-only"]), "portfolio": ("brain.portfolio.cli", ["build", portfolio_id]),
        "control": ("brain.control.cli", ["summary"]),
        "workspace": ("brain.workspace.cli", ["repositories", "list"]),
    }[group]
    team_state(monkeypatch)
    run(capsys, *lab(world["table-one"]))  # a team-state run leaves the marker
    assert cli_registry.marker_path(REGISTRY_DIR).is_file()
    database_down(monkeypatch)
    code, out, err = run(capsys, *command)
    assert code == 2, (out, err)
    assert "ERROR: Cannot reach the team database" in err and "may be stale" in err
    assert "WARNING" not in err


def test_brain_team_mode_refuses_the_file_without_a_marker(world, monkeypatch, capsys):
    database_down(monkeypatch)
    monkeypatch.setattr(settings, "BRAIN_TEAM_MODE", True)
    code, _, err = run(capsys, *freshness(world["file-only"]))
    assert code == 2 and "while BRAIN_TEAM_MODE is on" in err


def test_solo_state_removes_a_stale_marker(world, monkeypatch, capsys):
    cli_registry.mark_team_state(REGISTRY_DIR)
    team_state(monkeypatch, "solo")
    code, _, err = run(capsys, *lab(world["file-only"]))
    assert "lacks capability" in err
    assert not cli_registry.marker_path(REGISTRY_DIR).exists()


def test_team_state_never_falls_back_when_the_table_is_unreadable(world, monkeypatch, capsys):
    team_state(monkeypatch)
    broken = AsyncMock(side_effect=OSError("synthetic"))
    monkeypatch.setattr(shared, "ensure_legacy_imported", broken)
    code, _, err = run(capsys, *lab(world["file-only"]))
    assert code == 2 and "Cannot read the shared workspace registry in team state" in err


def test_team_state_reads_the_legacy_file_while_the_table_is_empty(tmp_path, monkeypatch, capsys):
    """The one-time import #149 does for the API runs for the offline CLIs too."""
    monkeypatch.chdir(tmp_path)
    root = (tmp_path / "legacy").resolve()
    root.mkdir()
    record = _record(root)
    RegistryStore(REGISTRY_DIR).register(record, actor="test", source="test")
    url = f"sqlite+aiosqlite:///{tmp_path}/empty.db"

    async def create() -> None:
        engine = create_async_engine(url, poolclass=NullPool)
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync: Base.metadata.create_all(
                sync, tables=[WorkspaceRegistryEntry.__table__, WorkspaceRegistryEvent.__table__]))
        await engine.dispose()

    asyncio.run(create())
    factory = async_sessionmaker(create_async_engine(url, poolclass=NullPool), expire_on_commit=False)
    monkeypatch.setattr("brain.database.session.async_session_factory", factory)
    team_state(monkeypatch)
    code, _, err = run(capsys, *lab(record.repository_id))
    assert "lacks capability" in err

    async def table_ids() -> set[str]:
        async with factory() as session:
            return {entry.repository_id for entry in await shared.table_records(session)}

    assert asyncio.run(table_ids()) == {record.repository_id}
