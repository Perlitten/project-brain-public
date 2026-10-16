import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.backup_runner import run_backup, main as backup_main
from scripts.restore_verification_drill import verify_restore_drill, main as drill_main
from scripts.recovery_config import load_recovery_config
from scripts.restore_database import FENCE_SQL, restore_database


@pytest.fixture(autouse=True)
def recovery_fixture_config(tmp_path, monkeypatch):
    config = tmp_path / "synthetic.env"
    config.write_text(
        "DATABASE_URL=postgresql+asyncpg://synthetic:fixture-only@127.0.0.1:25435/synthetic_db\n"
        "NEO4J_URI=bolt://127.0.0.1:25487\n"
        "NEO4J_USER=synthetic\nNEO4J_PASSWORD=fixture-only\n"
    )
    for key in (
        "DATABASE_URL", "POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER",
        "POSTGRES_PASSWORD", "POSTGRES_DB", "NEO4J_URI", "NEO4J_USER",
        "NEO4J_PASSWORD", "BRAIN_STORAGE_MODE",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("BRAIN_ENV_FILE", str(config))
    return config

REAL_DUMP = """-- PostgreSQL database dump
-- Dumped by pg_dump version 15.0

CREATE TABLE public.widgets (
    id integer NOT NULL,
    name text
);

COPY public.widgets (id, name) FROM stdin;
1\talpha
\\.

-- PostgreSQL database dump complete
"""

NEO4J_JSONL = (
    json.dumps({"kind": "node", "element_id": "4:abc:1", "labels": ["File"], "properties": {"path": "a.py"}})
    + "\n"
    + json.dumps({
        "kind": "relationship",
        "element_id": "5:abc:2",
        "type": "IMPORTS",
        "start": "4:abc:1",
        "end": "4:abc:3",
        "properties": {},
    })
    + "\n"
)


def _make_sqlite(path: Path, tables: int = 1) -> None:
    conn = sqlite3.connect(str(path))
    for i in range(tables):
        conn.execute(f"CREATE TABLE t{i} (id INTEGER)")
    conn.commit()
    conn.close()


def _fake_pg_dump(monkeypatch, tmpdir: Path):
    """Patch pg_dump presence + execution so the dump file is real content."""
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/pg_dump" if name == "pg_dump" else None)

    def _run(cmd, **kwargs):
        out_path = Path(cmd[cmd.index("-f") + 1])
        out_path.write_text(REAL_DUMP, encoding="utf-8")

        class _Done:
            returncode = 0
        return _Done()

    monkeypatch.setattr(subprocess, "run", _run)


def _fake_neo4j_export(monkeypatch, target_dir: Path):
    def _export(target: Path, uri: str, user: str, password: str, **kw):
        target.write_text(NEO4J_JSONL, encoding="utf-8")
        return {"nodes": 1, "relationships": 1}

    monkeypatch.setattr("scripts.backup_runner.export_neo4j_jsonl", _export)


class TestBackupRunner:
    def test_explicit_config_controls_both_endpoints_with_cron_environment(self, tmp_path, monkeypatch, recovery_fixture_config):
        captured = {}

        def pg(target, dsn):
            captured["pg"] = dsn
            target.write_text(REAL_DUMP)

        def graph(target, uri, user, password):
            captured["neo"] = (uri, user, password)
            target.write_text(NEO4J_JSONL)
            return {"nodes": 1, "relationships": 1}

        monkeypatch.setattr("scripts.backup_runner.export_postgres_dump", pg)
        monkeypatch.setattr("scripts.backup_runner.export_neo4j_jsonl", graph)
        assert run_backup(str(tmp_path / "backup"), config_path=str(recovery_fixture_config))["status"] == "ok"
        assert captured["pg"] == "postgresql://synthetic:fixture-only@127.0.0.1:25435/synthetic_db"
        assert captured["neo"] == ("bolt://127.0.0.1:25487", "synthetic", "fixture-only")

    def test_missing_config_fails_before_creating_any_artifact(self, tmp_path, monkeypatch):
        monkeypatch.delenv("BRAIN_ENV_FILE")
        with pytest.raises(ValueError, match="refusing implicit database defaults"):
            run_backup(str(tmp_path / "never-created"))
        assert not (tmp_path / "never-created").exists()

    def test_incomplete_config_and_ambiguous_dbname_option_fail_closed(self, tmp_path, recovery_fixture_config):
        recovery_fixture_config.write_text("POSTGRES_HOST=localhost\nNEO4J_URI=bolt://localhost:7687\n")
        with pytest.raises(ValueError, match="explicitly set"):
            load_recovery_config(str(recovery_fixture_config))
        recovery_fixture_config.write_text(
            "DATABASE_URL=postgresql://synthetic@localhost:25435/synthetic_db?dbname=other_db\n"
            "NEO4J_URI=bolt://localhost:25487\nNEO4J_USER=synthetic\nNEO4J_PASSWORD=fixture-only\n"
        )
        with pytest.raises(ValueError, match="true target"):
            load_recovery_config(str(recovery_fixture_config))

    def test_conflicting_environment_cannot_redirect_restore(self, recovery_fixture_config, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://synthetic@other-host/production")
        with pytest.raises(ValueError, match="ambiguous target"):
            load_recovery_config(str(recovery_fixture_config))

    def test_pg_dump_failure_does_not_write_password_to_manifest(self, tmp_path, monkeypatch):
        monkeypatch.setattr("scripts.backup_runner.shutil.which", lambda _: "/usr/bin/pg_dump")

        def failed(args, **kwargs):
            assert "fixture-only" not in str(args)
            assert kwargs["env"]["PGPASSWORD"] == "fixture-only"
            raise subprocess.CalledProcessError(1, args, stderr=b"fixture-only")

        monkeypatch.setattr("scripts.backup_runner.subprocess.run", failed)
        result = run_backup(str(tmp_path / "backup"), allow_missing=("neo4j",))
        assert result["status"] == "failed"
        assert "fixture-only" not in (tmp_path / "backup" / "backup_manifest.json").read_text()

    def test_missing_pg_dump_fails_visibly_no_placeholder(self, tmp_path, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)
        def _export(target: Path, *a, **k):
            target.write_text(NEO4J_JSONL, encoding="utf-8")
            return {"nodes": 1, "relationships": 0}

        monkeypatch.setattr("scripts.backup_runner.export_neo4j_jsonl", _export)

        manifest = run_backup(output_dir=str(tmp_path))

        assert manifest["status"] == "failed"
        pg = next(a for a in manifest["artifacts"] if a["name"] == "postgres")
        assert pg["status"] == "failed"
        assert pg["required"] is True
        assert not (tmp_path / "postgres_dump.sql").exists()
        # Every file the backup left behind must be a real artifact — never a marker.
        for f in tmp_path.iterdir():
            if f.name != "backup_manifest.json":
                assert "marker" not in f.read_text(errors="replace")[:200].lower()

    def test_cli_exit_nonzero_on_failed_backup(self, tmp_path, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)
        assert backup_main(["--output", str(tmp_path), "--allow-missing", "neo4j"]) == 1

    def test_successful_backup_records_real_artifacts(self, tmp_path, monkeypatch):
        _fake_pg_dump(monkeypatch, tmp_path)
        _fake_neo4j_export(monkeypatch, tmp_path)
        out = tmp_path / "backup"

        manifest = run_backup(output_dir=str(out))

        assert manifest["status"] == "ok"
        assert manifest["backup_version"] == "2.1.0"
        by_name = {a["name"]: a for a in manifest["artifacts"]}
        assert set(by_name) == {"postgres", "neo4j"}
        assert by_name["postgres"]["status"] == "ok" and by_name["postgres"]["sha256"]
        assert by_name["neo4j"]["status"] == "ok" and by_name["neo4j"]["record_counts"]["nodes"] == 1

    def test_backup_records_only_postgres_and_neo4j(self, tmp_path, monkeypatch):
        _fake_pg_dump(monkeypatch, tmp_path)
        _fake_neo4j_export(monkeypatch, tmp_path)

        manifest = run_backup(output_dir=str(tmp_path / "backup"))

        # the external orchestrator is gone as an artifact type (orchestration layer removed);
        # a backup produced today contains exactly the two real stores.
        assert [a["name"] for a in manifest["artifacts"]] == ["postgres", "neo4j"]
        assert not (tmp_path / "backup" / "orchestrator_database.sqlite").exists()
        assert manifest["status"] == "ok"

    def test_unknown_allow_missing_rejected(self, tmp_path):
        with pytest.raises(ValueError):
            run_backup(output_dir=str(tmp_path), allow_missing=("bogus",))


class TestRestoreDrill:
    def test_config_and_confirmation_gate_destructive_restore(self, tmp_path, recovery_fixture_config, monkeypatch):
        config = load_recovery_config(str(recovery_fixture_config))
        assert config.postgres_url.database == "synthetic_db"
        monkeypatch.setattr("scripts.restore_database.verify_restore_drill", lambda _: {
            "restore_drill_status": "artifacts_validated", "checks": {"postgres": {"violations": []}}
        })
        calls = []
        monkeypatch.setattr("scripts.restore_database.shutil.which", lambda _: "/usr/bin/psql")

        def run(args, **kw):
            calls.append((args, kw))
            # to_regclass finds the fence table, one fence is active: a team snapshot.
            return SimpleNamespace(stdout="t\n" if "to_regclass" in str(args) else "1\n")

        monkeypatch.setattr("scripts.restore_database.subprocess.run", run)
        (tmp_path / "postgres_dump.sql").write_text(REAL_DUMP)
        (tmp_path / "backup_manifest.json").write_text(json.dumps({"artifacts": [
            {"name": "postgres", "file": "postgres_dump.sql", "status": "ok"}
        ]}))
        with pytest.raises(ValueError, match="exactly match"):
            restore_database(tmp_path, str(recovery_fixture_config), "other_db")
        assert not calls
        assert restore_database(tmp_path, str(recovery_fixture_config), "synthetic_db") == "pending_reconciliation"
        assert len(calls) == 6
        # The dump and its fence load in one transaction, so the snapshot is never visible unfenced.
        load = calls[3][0]
        assert "--single-transaction" in load and load[-1] == str(FENCE_SQL) and load[-3].endswith("postgres_dump.sql")
        assert all("fixture-only" not in str(args) for args, _ in calls)
        assert all(kw["env"]["PGPASSWORD"] == "fixture-only" for _, kw in calls)
        assert "synthetic_db" in str(calls[1][0])

    def test_manifest_cannot_validate_a_different_postgres_file(self, tmp_path, recovery_fixture_config, monkeypatch):
        monkeypatch.setattr("scripts.restore_database.verify_restore_drill", lambda _: {
            "restore_drill_status": "artifacts_validated", "checks": {"postgres": {"violations": []}}
        })
        (tmp_path / "backup_manifest.json").write_text(json.dumps({"artifacts": [
            {"name": "postgres", "file": "../other.sql", "status": "ok"}
        ]}))
        with pytest.raises(ValueError, match="does not verify"):
            restore_database(tmp_path, str(recovery_fixture_config), "synthetic_db")

    def _write_manifest(self, backup_dir: Path, artifacts: list[dict], status="ok"):
        manifest = {
            "backup_version": "2.0.0",
            "backup_dir": str(backup_dir),
            "status": status,
            "artifacts": artifacts,
        }
        (backup_dir / "backup_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def _artifact(self, backup_dir: Path, name: str, content: bytes, *, required=True, status="ok"):
        file_name = {
            "postgres": "postgres_dump.sql",
            "neo4j": "neo4j_export.jsonl",
            "orchestrator": "orchestrator_database.sqlite",
        }[name]
        p = backup_dir / file_name
        p.write_bytes(content)
        return {
            "name": name,
            "file": file_name,
            "status": status,
            "required": required,
            "reason": "",
            "size_bytes": p.stat().st_size,
            "sha256": hashlib.sha256(content).hexdigest(),
        }

    def test_marker_only_backup_fails(self, tmp_path):
        # Reproduce the v1 bug exactly: marker files + SUCCESS report.
        self._write_manifest(tmp_path, [
            self._artifact(tmp_path, "postgres", b"-- pg_dump backup fallback marker\n-- error: boom\n"),
            self._artifact(tmp_path, "orchestrator", b"-- orchestrator sqlite backup marker\n"),
        ])
        report = verify_restore_drill(backup_dir=str(tmp_path))
        assert report["restore_drill_status"] == "failed"
        assert any("marker" in f for f in report["failures"])

    def test_failed_manifest_status_fails_drill(self, tmp_path):
        self._write_manifest(tmp_path, [
            {"name": "postgres", "file": None, "status": "failed", "required": True,
             "reason": "pg_dump not found", "size_bytes": 0, "sha256": None},
        ], status="failed")
        report = verify_restore_drill(backup_dir=str(tmp_path))
        assert report["restore_drill_status"] == "failed"

    def test_legacy_orchestrator_artifact_verified_by_checksum_only(self, tmp_path):
        # Backups written before the orchestrator removal still verify: the artifact is
        # checksum-checked like any other but gets no orchestrator-specific inspection.
        orch = tmp_path / "seed.sqlite"
        _make_sqlite(orch, tables=2)
        self._write_manifest(tmp_path, [
            self._artifact(tmp_path, "postgres", REAL_DUMP.encode()),
            self._artifact(tmp_path, "orchestrator", orch.read_bytes()),
        ])
        report = verify_restore_drill(backup_dir=str(tmp_path))
        assert report["restore_drill_status"] == "artifacts_validated"
        assert "orchestrator" not in report["checks"]
        assert report["failures"] == []

    def test_real_artifacts_validate_without_live_target(self, tmp_path):
        self._write_manifest(tmp_path, [
            self._artifact(tmp_path, "postgres", REAL_DUMP.encode()),
            self._artifact(tmp_path, "neo4j", NEO4J_JSONL.encode()),
        ])
        report = verify_restore_drill(backup_dir=str(tmp_path))
        assert report["restore_drill_status"] == "artifacts_validated"
        assert report["checksum_status"] == "ALL_MATCHED"
        assert report["checks"]["neo4j"]["record_counts"]["nodes"] == 1
        assert report["failures"] == []

    def test_checksum_mismatch_fails(self, tmp_path):
        artifact = self._artifact(tmp_path, "postgres", REAL_DUMP.encode())
        artifact["sha256"] = "0" * 64
        self._write_manifest(tmp_path, [artifact])
        report = verify_restore_drill(backup_dir=str(tmp_path))
        assert report["restore_drill_status"] == "failed"
        assert report["checksum_status"] == "MISMATCH"

    def test_cli_exit_code(self, tmp_path):
        self._write_manifest(tmp_path, [self._artifact(tmp_path, "postgres", REAL_DUMP.encode())])
        assert drill_main([str(tmp_path)]) == 0

    def test_live_restore_drives_psql_only(self, monkeypatch, tmp_path):
        """Regression: scratch create/drop run through psql — createdb/dropdb
        have no --dbname option (found by a live run against pg15)."""
        from scripts.restore_verification_drill import _live_restore_postgres

        calls = []

        class _Done:
            stdout = "19\n"
            stderr = ""

        monkeypatch.setattr(
            "scripts.restore_verification_drill.shutil.which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(
            "scripts.restore_verification_drill.subprocess.run",
            lambda args, **kw: calls.append(args) or _Done())

        dump = tmp_path / "postgres_dump.sql"
        dump.write_text("CREATE TABLE t(x int);")
        violations, stats = _live_restore_postgres(
            dump, "postgresql://u:p@dbhost:5433/brain_db")

        flat = [arg for call in calls for arg in call]
        assert not any("createdb" in a or "dropdb" in a for a in flat)
        assert violations == []
        assert stats["restored_tables"] == 19
        assert "postgresql://u:p@dbhost:5433/postgres" in calls[0]

    def test_live_restore_url_without_db_path_still_parses(self, monkeypatch, tmp_path):
        """Regression: --pg-url without a trailing database must not corrupt
        the host segment (naive rsplit produced a bogus hostname)."""
        from scripts.restore_verification_drill import _live_restore_postgres

        calls = []

        class _Done:
            stdout = "3\n"
            stderr = ""

        monkeypatch.setattr(
            "scripts.restore_verification_drill.shutil.which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(
            "scripts.restore_verification_drill.subprocess.run",
            lambda args, **kw: calls.append(args) or _Done())

        dump = tmp_path / "postgres_dump.sql"
        dump.write_text("CREATE TABLE t(x int);")
        violations, stats = _live_restore_postgres(
            dump, "postgresql://u:p@dbhost:5433")

        assert violations == []
        assert stats["scratch_database"].startswith("brain_restore_drill_")
        assert calls[0][2] == "postgresql://u:p@dbhost:5433/postgres"
        assert calls[1][2].startswith("postgresql://u:p@dbhost:5433/brain_restore_drill_")

    def test_live_restore_query_cannot_select_source_database(self, monkeypatch, tmp_path):
        from urllib.parse import parse_qs, urlsplit
        from scripts.restore_verification_drill import _live_restore_postgres

        calls = []
        monkeypatch.setattr("scripts.restore_verification_drill.shutil.which", lambda _: "/usr/bin/psql")
        monkeypatch.setattr("scripts.restore_verification_drill.subprocess.run",
                            lambda args, **kw: calls.append(args) or subprocess.CompletedProcess(args, 0, "3\n", ""))
        dump = tmp_path / "dump.sql"
        dump.write_text("CREATE TABLE t(x int);")
        url = "postgresql://u:p@dbhost/production?dbname=production&sslmode=require&dbname=other"
        violations, stats = _live_restore_postgres(dump, url)
        assert violations == []
        for call in calls:
            parts = urlsplit(call[2])
            assert "dbname" not in parse_qs(parts.query)
            assert parse_qs(parts.query)["sslmode"] == ["require"]
            assert parts.path in {"/postgres", "/" + stats["scratch_database"]}
        assert stats["scratch_database_dropped"] is True

    def test_live_restore_uses_short_unique_sql_safe_database_names(self, monkeypatch, tmp_path):
        import re
        from scripts.restore_verification_drill import _live_restore_postgres

        calls = []
        monkeypatch.setattr("scripts.restore_verification_drill.shutil.which", lambda _: "/usr/bin/psql")
        monkeypatch.setattr("scripts.restore_verification_drill.subprocess.run",
                            lambda args, **kw: calls.append(args) or subprocess.CompletedProcess(args, 0, "3\n", ""))
        dump = tmp_path / "dump.sql"
        dump.write_text("CREATE TABLE t(x int);")
        url = 'postgresql://u:p@dbhost/' + 'a' * 90 + '%22evil'
        names = [_live_restore_postgres(dump, url)[1]["scratch_database"] for _ in range(2)]
        assert names[0] != names[1]
        assert all(len(name) <= 63 and re.fullmatch(r"[a-z0-9_]+", name) for name in names)

    def test_failed_scratch_create_does_not_drop_a_database_or_leak_url(self, monkeypatch, tmp_path):
        from scripts.restore_verification_drill import _live_restore_postgres

        calls = []
        monkeypatch.setattr("scripts.restore_verification_drill.shutil.which", lambda _: "/usr/bin/psql")

        def fail(args, **kwargs):
            calls.append(args)
            raise subprocess.CalledProcessError(1, args, stderr="")

        monkeypatch.setattr("scripts.restore_verification_drill.subprocess.run", fail)
        violations, _ = _live_restore_postgres(tmp_path / "dump.sql", "postgresql://u:private-password@dbhost/source")
        assert violations and "private-password" not in str(violations)
        assert len(calls) == 1

    def test_restore_cleanup_failure_is_not_verified(self, monkeypatch, tmp_path):
        from scripts.restore_verification_drill import _live_restore_postgres

        monkeypatch.setattr("scripts.restore_verification_drill.shutil.which", lambda _: "/usr/bin/psql")

        def run(args, **kwargs):
            if args[-1].startswith("DROP DATABASE"):
                raise subprocess.CalledProcessError(1, args, stderr="cleanup failed")
            return subprocess.CompletedProcess(args, 0, "3\n", "")

        monkeypatch.setattr("scripts.restore_verification_drill.subprocess.run", run)
        violations, stats = _live_restore_postgres(tmp_path / "dump.sql", "postgresql://u:p@dbhost/source")
        assert "scratch database cleanup failed" in violations
        assert stats["scratch_database_dropped"] is False
