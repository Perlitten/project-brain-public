"""Verify a backup, then restore PostgreSQL to an explicitly confirmed database."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from sqlalchemy.engine import URL

from scripts.recovery_config import load_recovery_config
from scripts.restore_verification_drill import verify_restore_drill


def restore_database(backup_dir: Path, config_path: str | None, confirmed_database: str) -> None:
    config = load_recovery_config(config_path)
    database = config.postgres_url.database
    if not database or database != confirmed_database or database in {"postgres", "template0", "template1"}:
        raise ValueError("--confirm-database must exactly match the configured target (not a system database)")

    report = verify_restore_drill(str(backup_dir))
    if report["restore_drill_status"] != "artifacts_validated" or "postgres" not in report["checks"]:
        raise ValueError("Backup manifest/artifact verification failed; database was not changed")
    manifest = json.loads((backup_dir / "backup_manifest.json").read_text(encoding="utf-8"))
    if not any(item.get("name") == "postgres" and item.get("file") == "postgres_dump.sql"
               and item.get("status") == "ok" for item in manifest.get("artifacts", [])):
        raise ValueError("Backup manifest does not verify postgres_dump.sql; database was not changed")
    if not (backup_dir / "postgres_dump.sql").is_file():
        raise ValueError("PostgreSQL dump is missing; database was not changed")
    psql = shutil.which("psql")
    if not psql:
        raise ValueError("psql not found; database was not changed")

    def connection_url(name: str) -> str:
        source = config.postgres_url
        return URL.create("postgresql", username=source.username, host=source.host, port=source.port,
                          database=name, query=source.query).render_as_string(hide_password=False)

    env = {**os.environ, "PGPASSWORD": config.postgres_url.password or ""}
    quoted_database = database.replace('"', '""')
    literal_database = database.replace("'", "''")

    def execute(url: str, *args: str) -> None:
        try:
            subprocess.run([psql, "--dbname", url, "-X", "--set", "ON_ERROR_STOP=1", *args],
                           check=True, capture_output=True, timeout=300, env=env)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"PostgreSQL restore step failed (exit {exc.returncode}); check the target separately") from None

    admin = connection_url("postgres")
    execute(admin, "-c", f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='{literal_database}' AND pid<>pg_backend_pid()")
    execute(admin, "-c", f'DROP DATABASE IF EXISTS "{quoted_database}"')
    execute(admin, "-c", f'CREATE DATABASE "{quoted_database}"')
    execute(connection_url(database), "--single-transaction", "-f", str(backup_dir / "postgres_dump.sql"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup_dir", type=Path)
    parser.add_argument("--config", help="Brain .env path (or set BRAIN_ENV_FILE)")
    parser.add_argument("--confirm-database", required=True, help="Exact target DB name to drop and recreate")
    args = parser.parse_args(argv)
    try:
        restore_database(args.backup_dir, args.config, args.confirm_database)
    except (ValueError, RuntimeError) as exc:
        print(f"Restore failed: {exc}", file=sys.stderr)
        return 1
    print("PostgreSQL restored; Neo4j export is verified but NOT restored. Reindex the repository before restarting writes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
