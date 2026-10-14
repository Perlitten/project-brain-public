"""Acceptance checks against the actual publication workflow."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/release.yml"


def test_release_dispatch_is_gated_before_artifact_jobs():
    jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
    assert "preflight" in jobs
    assert 'scripts.release_gate --tag "$RELEASE_TAG"' in str(jobs["preflight"])
    assert "build_time" in jobs["preflight"]["outputs"]
    for name in ("dist", "images", "sbom"):
        assert jobs[name]["needs"] == "preflight"
        assert "needs.preflight.outputs.commit_sha" in str(jobs[name])


def test_all_images_require_verifiable_identity():
    workflow = WORKFLOW.read_text()
    assert "BRAIN_BUILD_SHA=${{ needs.preflight.outputs.commit_sha }}" in workflow
    assert "BRAIN_REQUIRE_RELEASE_IDENTITY=true" in workflow
    dockerfile = (ROOT / "apps/web/Dockerfile").read_text()
    assert "org.opencontainers.image.revision=${BRAIN_BUILD_SHA}" in dockerfile
    assert "^[0-9a-f]{40}$" in dockerfile


def test_sboms_cover_installed_images_not_only_source():
    workflow = WORKFLOW.read_text()
    assert "image: ghcr.io/perlitten/project-brain-${{ matrix.name }}:" in workflow
    assert workflow.count("upload-release-assets: false") == 2
    assert "scripts/check_image_sbom.py" in workflow
    for kind in ("source", "api", "worker", "web"):
        assert f"sbom-{kind}.spdx.json" in workflow


def test_publication_uses_the_tested_distribution_bytes():
    workflow = WORKFLOW.read_text()
    assert workflow.count("python -m build") == 1
    release = yaml.safe_load(workflow)["jobs"]["github-release"]
    assert "actions/download-artifact@" in str(release)
    assert "sha256sum -c SHA256SUMS" in str(release)
    assert "sbom/*.sha256" in str(release)
    assert "scripts.release_gate --tag" in str(release)
