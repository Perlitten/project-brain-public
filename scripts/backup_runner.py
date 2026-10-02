#!/usr/bin/env python3
"""Automated Backup Runner for Project Brain.

Exports PostgreSQL schemas/embeddings, Neo4j graph data, and n8n sqlite metadata.
Generates an explicit manifest with SHA-256 checksums and per-artifact status.

Truthfulness contract: an artifact is only recorded as ``ok`` when a real export
was produced. Missing tools, failed exports, and absent required stores are
recorded as ``failed``/``absent_source`` and make the overall manifest status
``failed`` (non-zero exit). No placeholder/marker files are ever written.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

BACKUP_VERSION = "2.0.0"
ARTIFACT_POSTGRES = "postgres"
ARTIFACT_NEO4J = "neo4j"
ARTIFACT_N8N = "n8n"
ARTIFACT_NAMES = (ARTIFACT_POSTGRES, ARTIFACT_NEO4J, ARTIFACT_N8N)


def calculate_sha256(filepath: Path) -> str:
    sha = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            sha.update(chunk)
    return sha.hexdigest()


def _artifact_ok(name: str, file_path: Path) -> dict:
    return {
        "name": name,
        "file": file_path.name,
        "status": "ok",
        "required": True,
        "reason": "",
        "size_bytes": file_path.stat().st_size,
        "sha256": calculate_sha256(file_path),
    }


def _artifact_failed(name: str, reason: str, *, required: bool = True) -> dict:
    return {
        "name": name,
        "file": None,
        "status": "failed",
        "required": required,
        "reason": reason,
        "size_bytes": 0,
        "sha256": None,
    }


def _artifact_skipped(name: str, status: str, reason: str) -> dict:
    return {
        "name": name,
        "file": None,
        "status": status,
        "required": False,
        "reason": reason,
        "size_bytes": 0,
        "sha256": None,
    }


def export_postgres_dump(target: Path, dsn: str, *, timeout: int = 300) -> None:
    """Run pg_dump into ``target``. Raises on missing tool or failed export."""
    pg_dump = shutil.which("pg_dump")
    if not pg_dump:
        raise RuntimeError("pg_dump not found on PATH; cannot produce a real PostgreSQL backup")
    subprocess.run([pg_dump, "--dbname", dsn, "-f", str(target)], check=True, timeout=timeout)
    if not target.exists() or target.stat().st_size == 0:
        raise RuntimeError("pg_dump produced an empty dump")


def export_neo4j_jsonl(target: Path, uri: str, user: str, password: str, *, batch_size: int = 5000) -> dict:
    """Export all nodes and relationships to JSONL via the neo4j driver."""
    try:
        from neo4j import GraphDatabase
    except ImportError as exc:  # pragma: no cover - dependency is installed in prod
        raise RuntimeError(f"neo4j python driver unavailable: {exc}") from exc

    driver = GraphDatabase.driver(uri, auth=(user, password))
    nodes = 0
    rels = 0
    try:
        with target.open("w", encoding="utf-8") as out:
            with driver.session() as session:
                result = session.run("MATCH (n) RETURN n")
                for record in result:
                    node = record["n"]
                    out.write(json.dumps({
                        "kind": "node",
                        "element_id": node.element_id,
                        "labels": list(node.labels),
                        "properties": dict(node),
                    }, default=str) + "\n")
                    nodes += 1
                result = session.run("MATCH ()-[r]->() RETURN r")
                for record in result:
                    rel = record["r"]
                    out.write(json.dumps({
                        "kind": "relationship",
                        "element_id": rel.element_id,
                        "type": rel.type,
                        "start": rel.start_node.element_id,
                        "end": rel.end_node.element_id,
                        "properties": dict(rel),
                    }, default=str) + "\n")
                    rels += 1
    finally:
        driver.close()
    if nodes == 0 and rels == 0:
        target.unlink(missing_ok=True)
        raise RuntimeError("neo4j export produced zero records — refusing to record an empty graph backup")
    return {"nodes": nodes, "relationships": rels}


def run_backup(
    output_dir: str | None = None,
    *,
    pg_dsn: str | None = None,
    n8n_source: str | None = None,
    neo4j_uri: str | None = None,
    neo4j_user: str | None = None,
    neo4j_password: str | None = None,
    allow_missing: tuple[str, ...] = (),
) -> dict:
    """Produce real backup artifacts and a manifest describing exactly what was captured.

    Every store reports an explicit status; the backup fails (``status: failed``)
    whenever a required artifact could not be produced. ``allow_missing`` demotes
    named artifacts (``postgres``, ``neo4j``, ``n8n``) to optional for
    environments where that store is genuinely out of scope.
    """
    unknown = set(allow_missing) - set(ARTIFACT_NAMES)
    if unknown:
        raise ValueError(f"Unknown artifact(s) in allow_missing: {sorted(unknown)}")

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    target_path = Path(output_dir) if output_dir else Path("backups") / f"brain_backup_{ts}"
    target_path.mkdir(parents=True, exist_ok=True)

    artifacts: list[dict] = []

    # 1. PostgreSQL (required unless explicitly allowed missing)
    pg_file = target_path / "postgres_dump.sql"
    if ARTIFACT_POSTGRES in allow_missing:
        artifacts.append(_artifact_skipped(ARTIFACT_POSTGRES, "skipped", "demoted by --allow-missing postgres"))
    else:
        try:
            export_postgres_dump(pg_file, pg_dsn or os.environ.get(
                "DATABASE_URL", "postgresql://brain:brain_password@localhost:5432/brain_db"))
            artifacts.append(_artifact_ok(ARTIFACT_POSTGRES, pg_file))
        except Exception as exc:
            pg_file.unlink(missing_ok=True)
            artifacts.append(_artifact_failed(ARTIFACT_POSTGRES, str(exc)))

    # 2. Neo4j graph (required unless explicitly allowed missing)
    neo4j_file = target_path / "neo4j_export.jsonl"
    if ARTIFACT_NEO4J in allow_missing:
        artifacts.append(_artifact_skipped(ARTIFACT_NEO4J, "skipped", "demoted by --allow-missing neo4j"))
    else:
        try:
            counts = export_neo4j_jsonl(
                neo4j_file,
                neo4j_uri or os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
                neo4j_user or os.environ.get("NEO4J_USER", "neo4j"),
                neo4j_password or os.environ.get("NEO4J_PASSWORD", "neo4j_password"),
            )
            entry = _artifact_ok(ARTIFACT_NEO4J, neo4j_file)
            entry["record_counts"] = counts
            artifacts.append(entry)
        except Exception as exc:
            neo4j_file.unlink(missing_ok=True)
            artifacts.append(_artifact_failed(ARTIFACT_NEO4J, str(exc)))

    # 3. n8n sqlite (optional store: absent source is recorded, never faked)
    n8n_db = Path(n8n_source) if n8n_source else Path("n8n_data/database.sqlite")
    n8n_file = target_path / "n8n_database.sqlite"
    if ARTIFACT_N8N in allow_missing:
        artifacts.append(_artifact_skipped(ARTIFACT_N8N, "skipped", "demoted by --allow-missing n8n"))
    elif n8n_db.exists():
        shutil.copy2(n8n_db, n8n_file)
        artifacts.append(_artifact_ok(ARTIFACT_N8N, n8n_file))
    else:
        artifacts.append(_artifact_skipped(
            ARTIFACT_N8N, "absent_source",
            f"n8n sqlite not found at {n8n_db}; store not present in this deployment",
        ))

    status = "ok" if all(a["status"] == "ok" for a in artifacts if a["required"]) else "failed"
    manifest = {
        "backup_version": BACKUP_VERSION,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "backup_dir": str(target_path.resolve()),
        "status": status,
        "artifacts": artifacts,
    }

    manifest_file = target_path / "backup_manifest.json"
    with open(manifest_file, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    if status == "ok":
        print(f"Backup completed successfully: {target_path}")
    else:
        failed = [a["name"] for a in artifacts if a["required"] and a["status"] != "ok"]
        print(f"Backup FAILED — required artifact(s) not produced: {', '.join(failed)}: {target_path}", file=sys.stderr)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Project Brain Backup Runner")
    parser.add_argument("--output", help="Output directory for backup artifacts")
    parser.add_argument(
        "--allow-missing",
        action="append",
        default=[],
        choices=ARTIFACT_NAMES,
        help="Demote a store to optional when it is out of scope for this deployment",
    )
    parser.add_argument("--pg-dsn", help="PostgreSQL DSN (default: $DATABASE_URL)")
    parser.add_argument("--n8n-source", help="Path to n8n database.sqlite")
    parser.add_argument("--neo4j-uri", help="Neo4j bolt URI (default: $NEO4J_URI)")
    parser.add_argument("--neo4j-user", help="Neo4j user (default: $NEO4J_USER)")
    parser.add_argument("--neo4j-password", help="Neo4j password (default: $NEO4J_PASSWORD)")
    args = parser.parse_args(argv)
    manifest = run_backup(
        args.output,
        pg_dsn=args.pg_dsn,
        n8n_source=args.n8n_source,
        neo4j_uri=args.neo4j_uri,
        neo4j_user=args.neo4j_user,
        neo4j_password=args.neo4j_password,
        allow_missing=tuple(args.allow_missing),
    )
    return 0 if manifest["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
