"""Reject misleading mutation runs before copying or executing any test source."""
import hashlib

import pytest

from scripts.mutation_audit import manifest, run


def test_generated_manifest_fingerprints_actual_crlf_source_bytes(tmp_path, monkeypatch):
    import scripts.mutation_audit as module

    source = b"def choose():\r\n    return False\r\n"
    (tmp_path / "source.py").write_bytes(source)
    monkeypatch.setattr(module, "SCOPE", {"source.py": {"choose"}})
    record = manifest(tmp_path)
    assert record["source_sha256"]["source.py"] == hashlib.sha256(source).hexdigest()


def test_wrong_source_manifest_fails_before_any_test_runs(tmp_path, monkeypatch):
    import scripts.mutation_audit as module

    (tmp_path / "source.py").write_text("changed source", encoding="utf-8")
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: pytest.fail("must not execute tests"))
    frozen = {"source_sha256": {"source.py": hashlib.sha256(b"original source").hexdigest()}, "mutations": []}
    output = tmp_path / "results"
    with pytest.raises(ValueError, match="source hashes differ"):
        run(tmp_path, frozen, output, [], 1, 1)
    assert not output.exists()


def test_explicit_empty_replay_records_actual_source_hashes(tmp_path, monkeypatch):
    import json
    import scripts.mutation_audit as module
    from types import SimpleNamespace

    (tmp_path / "source.py").write_text("changed source", encoding="utf-8")
    for name in ("brain", "apps", "tests", "rules", "eval"):
        (tmp_path / name).mkdir()
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    frozen = {"source_sha256": {"source.py": hashlib.sha256(b"original source").hexdigest()}, "mutations": []}
    output = tmp_path / "results"
    run(tmp_path, frozen, output, [], 1, 1, replay=True)
    record = json.loads((output / "results.json").read_text())
    assert record["replay"] is True
    assert record["source_mismatches"] == ["source.py"]
    assert record["tested_source_sha256"] == {"source.py": hashlib.sha256(b"changed source").hexdigest()}
    assert record["results"] == []
