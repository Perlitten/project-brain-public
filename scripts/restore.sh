#!/usr/bin/env bash
# Project Brain — restore from a backup_runner.py backup directory.
#
#   ./scripts/restore.sh /path/to/brain_backup_YYYYMMDD_HHMMSS
#
# What it does:
#   1. verifies backup_manifest.json (status must be "ok", checksums match)
#   2. restores PostgreSQL from postgres_dump.sql (drops and recreates brain_db)
#   3. prints the neo4j recovery step (graph is derived — reindex rebuilds it)
#
# Run this with services up (docker compose up -d postgres redis neo4j).
# The API/worker should be STOPPED during restore to avoid writes mid-restore.
set -euo pipefail

BACKUP_DIR="${1:?usage: $0 /path/to/brain_backup_YYYYMMDD_HHMMSS}"
MANIFEST="$BACKUP_DIR/backup_manifest.json"

[[ -f "$MANIFEST" ]] || { echo "no manifest at $MANIFEST" >&2; exit 1; }

echo "==> Verifying manifest"
STATUS=$(python3 -c "import json; print(json.load(open('$MANIFEST'))['status'])")
[[ "$STATUS" == "ok" ]] || { echo "backup status is '$STATUS', refusing to restore" >&2; exit 1; }

echo "==> Verifying checksums"
python3 - "$BACKUP_DIR" <<'EOF'
import hashlib, json, sys
d = sys.argv[1]
m = json.load(open(f"{d}/backup_manifest.json"))
ok = True
for a in m["artifacts"]:
    if a["status"] != "ok" or not a.get("required", True):
        continue
    p = f"{d}/{a['file']}"
    h = hashlib.sha256(open(p, "rb").read()).hexdigest()
    if h != a["sha256"]:
        print(f"CHECKSUM MISMATCH: {a['file']}", file=sys.stderr)
        ok = False
    else:
        print(f"  ok {a['file']}")
sys.exit(0 if ok else 1)
EOF

echo "==> Restoring PostgreSQL"
PG_DUMP="$BACKUP_DIR/postgres_dump.sql"
[[ -f "$PG_DUMP" ]] || { echo "no postgres_dump.sql in backup" >&2; exit 1; }

export PGHOST="${POSTGRES_HOST:-localhost}"
export PGPORT="${POSTGRES_PORT:-5433}"
export PGUSER="${POSTGRES_USER:-postgres}"
export PGPASSWORD="${POSTGRES_PASSWORD:-postgres}"

echo "  dropping and recreating brain_db (all current data will be lost)"
read -r -p "  type RESTORE to confirm: " CONFIRM
[[ "$CONFIRM" == "RESTORE" ]] || { echo "aborted"; exit 1; }

psql -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='brain_db' AND pid <> pg_backend_pid();" postgres >/dev/null
psql -c "DROP DATABASE IF EXISTS brain_db;" postgres
psql -c "CREATE DATABASE brain_db;" postgres
psql -d brain_db -f "$PG_DUMP" >/dev/null
echo "  postgres restored"

echo
echo "==> Neo4j"
echo "  The graph is derived from the indexed repository — it is rebuilt by reindexing,"
echo "  not from the JSONL export (element_ids are not stable across databases)."
echo "  After starting the API, run:"
echo "    .venv/bin/brain index --repo /path/to/your/repo --clean"
echo
echo "Restore complete. Start the API/worker when ready."
