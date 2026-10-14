"""Offline release gate tests; these do not claim live GitHub publishing."""

from unittest.mock import MagicMock
import urllib.error

import pytest
import tomllib

from scripts import check_image_sbom, release_gate


SHA = "a" * 40


def run(**values):
    return {
        "id": 1, "head_sha": SHA, "event": "push", "head_branch": "main",
        "path": ".github/workflows/ci.yml", "status": "completed", "conclusion": "success",
        **values,
    }


def test_exact_main_push_ci_passes():
    release_gate.verified_ci_runs({"workflow_runs": [run()]}, SHA)


@pytest.mark.parametrize("overrides", [
    {"head_sha": "b" * 40}, {"event": "pull_request"}, {"head_branch": "topic"},
    {"path": ".github/workflows/release.yml"}, {"status": "in_progress"},
    {"conclusion": "failure"}, {"conclusion": "cancelled"},
])
def test_other_or_non_green_ci_fails_closed(overrides):
    with pytest.raises(ValueError):
        release_gate.verified_ci_runs({"workflow_runs": [run(**overrides)]}, SHA)


def test_latest_failure_does_not_reuse_older_green_run():
    with pytest.raises(ValueError):
        release_gate.verified_ci_runs({"workflow_runs": [run(), run(id=2, conclusion="failure")]}, SHA)


def test_missing_ci_fails_closed():
    with pytest.raises(ValueError):
        release_gate.verified_ci_runs({"workflow_runs": []}, SHA)


def test_existing_release_is_not_replaced(monkeypatch):
    monkeypatch.setattr(release_gate.urllib.request, "urlopen", lambda *args, **kwargs: MagicMock())
    with pytest.raises(ValueError, match="refusing asset replacement"):
        release_gate.require_unpublished_release("example/repository", "v0.9.2", "synthetic")


@pytest.mark.parametrize("status", [403, 500])
def test_release_lookup_errors_fail_closed(monkeypatch, status):
    def fail(*args, **kwargs):
        raise urllib.error.HTTPError("https://example.invalid", status, "synthetic", None, None)
    monkeypatch.setattr(release_gate.urllib.request, "urlopen", fail)
    with pytest.raises(urllib.error.HTTPError):
        release_gate.require_unpublished_release("example/repository", "v0.9.2", "synthetic")


def test_only_a_confirmed_missing_release_can_proceed(monkeypatch):
    def missing(*args, **kwargs):
        raise urllib.error.HTTPError("https://example.invalid", 404, "synthetic", None, None)
    monkeypatch.setattr(release_gate.urllib.request, "urlopen", missing)
    release_gate.require_unpublished_release("example/repository", "v0.9.2", "synthetic")


def test_exact_pushed_tag_is_required(monkeypatch):
    monkeypatch.setattr(release_gate.subprocess, "check_output", lambda *a, **k: SHA)
    release_gate.verified_tag("v0.9.2", SHA, "0.9.2")
    with pytest.raises(ValueError):
        release_gate.verified_tag("v0.9.2", "b" * 40, "0.9.2")


@pytest.mark.parametrize("tag", ["main", "v0.9.3", "v0.9.2;echo unsafe", "v0.9.2-rc1"])
def test_arbitrary_dispatch_or_mismatched_versions_are_rejected(tag):
    with pytest.raises(ValueError):
        release_gate.verified_tag(tag, SHA, "0.9.2")


def test_source_digest_tracks_the_copied_lock(monkeypatch, tmp_path):
    monkeypatch.setattr(release_gate, "ROOT", tmp_path)
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("COPY pyproject.toml README.md ./\n")
    assert "requirements.lock" not in release_gate.source_inputs()
    dockerfile.write_text("COPY pyproject.toml requirements.lock README.md ./\n")
    assert "requirements.lock" in release_gate.source_inputs()


def test_source_only_inventory_cannot_satisfy_backend_sbom():
    with pytest.raises(ValueError):
        check_image_sbom.validate_image_sbom({"packages": [{"name": "next", "versionInfo": "synthetic"}]}, "api")


def test_complete_installed_backend_inventory_passes():
    project = tomllib.loads((check_image_sbom.ROOT / "pyproject.toml").read_text())["project"]
    names = [dependency.split("[")[0].split(">=")[0].split("==")[0] for dependency in project["dependencies"]]
    packages = [{"name": name, "versionInfo": "synthetic"} for name in names]
    packages.append({"name": "project-brain", "versionInfo": project["version"]})
    check_image_sbom.validate_image_sbom({"packages": packages}, "worker")


def test_web_inventory_requires_installed_versions():
    with pytest.raises(ValueError):
        check_image_sbom.validate_image_sbom({"packages": [{"name": "next"}, {"name": "react"}]}, "web")
