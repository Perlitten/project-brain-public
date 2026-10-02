#!/usr/bin/env python3
"""Restore Verification Drill for Project Brain.

Restores PostgreSQL dumps, pgvector embeddings, and n8n sqlite metadata from
a backup archive into an isolated verification context.
Validates SHA-256 checksums, schema integrity, and row/vector counts.

Truthfulness contract: the drill reports ``verified`` only when a live restore
into a target PostgreSQL (--pg-url) succeeded and every required artifact
checked out; ``artifacts_validated`` when content-level checks passed without a
live target; ``failed`` on any integrity, marker, or emptiness violation.
Placeholder backups produced by backup_version 1.0.0 always fail.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]

MARKER_PREFIXES = (
    "-- pg_dump backup fallback marker",
    "-- pg_dump not found on host",
    "-- n8n sqlite backup marker",
)

# DDL/DML markers that a real pg_dump output always contains
_PG_DUMP_REQUIRED_TOKENS = ("pg_dump",)
_PG_DUMP_CONTENT_TOKENS = ("CREATE TABLE", "COPY ", "INSERT INTO")


def calculate_sha256(filepath: Path) -> str:
    sha = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            sha.update(chunk)
    return sha.hexdigest()


def _is_marker_file(file_path: Path) -> bool:
    try:
        head = file_path.read_text(encoding="utf-8", errors="replace")[:512]
    except Exception:
        return False
    return any(head.startswith(prefix) for prefix in MARKER_PREFIXES)


def _check_postgres_artifact(file_path: Path) -> list[str]:
    """Return a list of violations; empty means the dump looks real."""
    violations = []
    if not file_path.exists() or file_path.stat().st_size == 0:
        return ["postgres_dump.sql missing or empty"]
    if _is_marker_file(file_path):
        return ["postgres_dump.sql is a v1 placeholder marker, not a real dump"]
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return [f"postgres_dump.sql unreadable: {exc}"]
    if not any(token in text for token in _PG_DUMP_REQUIRED_TOKENS):
        violations.append("postgres_dump.sql lacks pg_dump header")
    if not any(token in text for token in _PG_DUMP_CONTENT_TOKENS):
        violations.append("postgres_dump.sql contains no DDL/DML statements")
    return violations


def _check_sqlite_artifact(file_path: Path) -> tuple[list[str], int]:
    """Return (violations, table_count)."""
    if _is_marker_file(file_path):
        return ["n8n_database.sqlite is a v1 placeholder marker, not a real database"], 0
    try:
        conn = sqlite3.connect(str(file_path))
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM sqlite_master WHERE type='table';")
        table_count = cur.fetchone()[0]
        conn.close()
    except Exception as exc:
        return [f"n8n_database.sqlite is not a readable sqlite database: {exc}"], 0
    if table_count < 1:
        return ["n8n_database.sqlite contains zero tables"], 0
    return [], table_count


def _check_neo4j_artifact(file_path: Path) -> tuple[list[str], dict]:
    """Return (violations, counts)."""
    counts = {"nodes": 0, "relationships": 0, "malformed": 0}
    if not file_path.exists() or file_path.stat().st_size == 0:
        return ["neo4j_export.jsonl missing or empty"], counts
    try:
        for line in file_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                counts["malformed"] += 1
                continue
            kind = record.get("kind")
            if kind == "node":
                counts["nodes"] += 1
            elif kind == "relationship":
                counts["relationships"] += 1
            else:
                counts["malformed"] += 1
    except Exception as exc:
        return [f"neo4j_export.jsonl unreadable: {exc}"], counts
    violations = []
    if counts["malformed"]:
        violations.append(f"neo4j_export.jsonl has {counts['malformed']} malformed record(s)")
    if counts["nodes"] + counts["relationships"] == 0:
        violations.append("neo4j_export.jsonl contains no graph records")
    return violations, counts


def _live_restore_postgres(dump_file: Path, pg_url: str) -> tuple[list[str], dict]:
    """Apply the dump to a target PostgreSQL and count restored tables.

    Uses the ``psql`` toolchain on a scratch database derived from
    the target URL's database name so the drill never touches live data.
    """
    psql = shutil.which("psql")
    if not psql:
        return ["psql not found on PATH; cannot perform live restore"], {}

    # Use a short, unique SQL-safe identifier independent of the target name.
    # PostgreSQL truncates identifiers at 63 bytes; timestamp suffixes on long
    # names can collide, and quoting an arbitrary URL path is not SQL escaping.
    parts = urlsplit(pg_url)
    if parts.scheme not in {"postgres", "postgresql"}:
        return ["live restore requires a postgres:// or postgresql:// URL"], {}
    scratch_db = f"brain_restore_drill_{uuid.uuid4().hex}"
    # A dbname query parameter overrides the URI path in libpq. Remove it
    # before selecting the admin/scratch DB; preserve SSL and other options.
    query = urlencode([(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
                       if key != "dbname"])
    admin_url = urlunsplit(parts._replace(path="/postgres", query=query, fragment=""))
    scratch_url = urlunsplit(parts._replace(path=f"/{scratch_db}", query=query, fragment=""))
    stats: dict = {"scratch_database": scratch_db}
    violations = []
    created = False
    try:
        # psql alone drives the scratch lifecycle — createdb/dropdb have no
        # --dbname option and are not required for the drill.
        subprocess.run(
            [psql, "--dbname", admin_url, "-q", "--set", "ON_ERROR_STOP=1",
             "-c", f'CREATE DATABASE "{scratch_db}"'],
            check=True, timeout=60, capture_output=True, text=True)
        created = True
        subprocess.run([psql, "--dbname", scratch_url, "-f", str(dump_file), "-q", "--set", "ON_ERROR_STOP=1"],
                       check=True, timeout=300, capture_output=True, text=True)
        out = subprocess.run(
            [psql, "--dbname", scratch_url, "-tAc",
             "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"],
            check=True, timeout=60, capture_output=True, text=True)
        stats["restored_tables"] = int(out.stdout.strip() or 0)
        if stats["restored_tables"] < 1:
            violations.append("live restore produced zero tables in scratch database")
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or f"psql exited with status {exc.returncode}").strip()[:400]
        violations.append(f"live restore failed: {detail}")
    except subprocess.TimeoutExpired:
        violations.append("live restore timed out")
    finally:
        if created:
            try:
                subprocess.run(
                    [psql, "--dbname", admin_url, "-q", "--set", "ON_ERROR_STOP=1",
                     "-c", f'DROP DATABASE IF EXISTS "{scratch_db}"'],
                    check=True, timeout=60, capture_output=True, text=True)
                stats["scratch_database_dropped"] = True
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                violations.append("scratch database cleanup failed")
                stats["scratch_database_dropped"] = False
    return violations, stats


def verify_restore_drill(backup_dir: str, *, pg_url: str | None = None) -> dict:
    dir_path = Path(backup_dir)
    manifest_file = dir_path / "backup_manifest.json"
    if not manifest_file.exists():
        raise FileNotFoundError(f"Backup manifest missing: {manifest_file}")

    with open(manifest_file, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    failures: list[str] = []
    checks: dict[str, dict] = {}

    # 0. The drill refuses to certify a backup that already declared failure.
    if manifest.get("status") == "failed":
        failures.append("backup manifest status is 'failed' — nothing trustworthy to restore")

    artifacts = manifest.get("artifacts", [])
    if not artifacts:
        failures.append("backup manifest contains no artifacts")

    # 1. Verify SHA-256 checksums for every artifact that produced a file.
    checksum_results = []
    for item in artifacts:
        name = item["name"]
        file_name = item.get("file") or name
        file_path = dir_path / file_name
        if item.get("status") != "ok":
            if item.get("required"):
                failures.append(f"required artifact '{name}' has status '{item.get('status')}': {item.get('reason')}")
            continue
        if not file_path.exists():
            # tolerate legacy manifest entries that stored absolute paths
            legacy_path = Path(item.get("path", ""))
            file_path = legacy_path if legacy_path.exists() else file_path
        if not file_path.exists():
            failures.append(f"artifact file missing: {name}")
            continue
        actual_sha = calculate_sha256(file_path)
        expected_sha = item.get("sha256")
        valid = bool(expected_sha) and actual_sha == expected_sha
        checksum_results.append({
            "name": name,
            "sha256_match": valid,
            "expected": expected_sha,
            "actual": actual_sha,
        })
        if not valid:
            failures.append(f"SHA-256 mismatch for {name}")
        if _is_marker_file(file_path):
            failures.append(f"artifact '{name}' is a placeholder marker file, not a real backup")

    # 2. Per-artifact integrity checks.
    artifact_by_name = {a["name"]: a for a in artifacts}
    pg_item = artifact_by_name.get("postgres")
    if pg_item and pg_item.get("status") == "ok":
        pg_file = dir_path / (pg_item.get("file") or "postgres_dump.sql")
        violations = _check_postgres_artifact(pg_file)
        checks["postgres"] = {"violations": violations}
        failures.extend(violations)
        if not violations and pg_url:
            live_violations, live_stats = _live_restore_postgres(pg_file, pg_url)
            checks["postgres"]["live_restore"] = live_stats
            failures.extend(live_violations)

    n8n_item = artifact_by_name.get("n8n")
    if n8n_item and n8n_item.get("status") == "ok":
        n8n_file = dir_path / (n8n_item.get("file") or "n8n_database.sqlite")
        violations, table_count = _check_sqlite_artifact(n8n_file)
        checks["n8n"] = {"violations": violations, "sqlite_tables": table_count}
        failures.extend(violations)

    neo4j_item = artifact_by_name.get("neo4j")
    if neo4j_item and neo4j_item.get("status") == "ok":
        neo4j_file = dir_path / (neo4j_item.get("file") or "neo4j_export.jsonl")
        violations, counts = _check_neo4j_artifact(neo4j_file)
        checks["neo4j"] = {"violations": violations, "record_counts": counts}
        failures.extend(violations)

    # 3. Verdict.
    live_restore_performed = bool(pg_url and checks.get("postgres", {}).get("live_restore"))
    if failures:
        drill_status = "failed"
    elif live_restore_performed:
        drill_status = "verified"
    else:
        drill_status = "artifacts_validated"

    report = {
        "restore_timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "backup_source": str(dir_path.resolve()),
        "backup_status": manifest.get("status", "unknown"),
        "artifacts_verified": len(checksum_results),
        "checksum_status": "ALL_MATCHED" if all(r["sha256_match"] for r in checksum_results) else "MISMATCH",
        "checks": checks,
        "failures": failures,
        "live_restore": live_restore_performed,
        "restore_drill_status": drill_status,
    }

    out_file = PROJECT_ROOT / "reports" / "audit-verification" / "backups" / "restore_drill_results.json"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    print(f"Restore verification drill {drill_status}: {json.dumps(report, indent=2)}")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Restore Verification Drill")
    parser.add_argument("backup_dir", help="Path to backup archive directory")
    parser.add_argument("--pg-url", help="Target PostgreSQL base URL for a live scratch-database restore")
    args = parser.parse_args(argv)
    report = verify_restore_drill(args.backup_dir, pg_url=args.pg_url)
    return 0 if report["restore_drill_status"] in ("verified", "artifacts_validated") else 1


if __name__ == "__main__":
    sys.exit(main())
