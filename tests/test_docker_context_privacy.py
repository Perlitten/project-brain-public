from pathlib import Path

import pytest

from scripts.write_source_manifest import build_manifest


ROOT = Path(__file__).resolve().parents[1]
DATABASE_NAMES = (
    "local.db", "local.db-wal", "local.db-shm", "local.db-journal",
    "local.sqlite", "local.sqlite-wal", "local.sqlite3", "local.sqlite3-shm",
)


@pytest.mark.parametrize("filename", DATABASE_NAMES)
def test_nested_database_is_neither_in_context_nor_source_identity(tmp_path, filename):
    subtree = tmp_path / "apps" / "mcp_server"
    subtree.mkdir(parents=True)
    (subtree / "safe.py").write_text("pass\n")
    expected = build_manifest(tmp_path, includes=["apps"])["content_digest"]
    (subtree / filename).write_text("synthetic-private-database-sentinel")
    assert build_manifest(tmp_path, includes=["apps"])["content_digest"] == expected

    patterns = (ROOT / ".dockerignore").read_text().splitlines()
    suffix = filename.removeprefix("local")
    for sidecar in ("-wal", "-shm", "-journal"):
        if suffix.endswith(sidecar):
            suffix = suffix[:-len(sidecar)] + "-*"
            break
    assert f"**/*{suffix}" in patterns


def test_private_database_rules_follow_source_includes():
    patterns = (ROOT / ".dockerignore").read_text().splitlines()
    assert patterns.index("**/*.db") > patterns.index("!apps/**")
