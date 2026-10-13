"""Release archives must reject generated outputs and local configuration."""

from pathlib import Path
import tarfile
import zipfile

import pytest

from scripts.check_release_archives import check_archive, unsafe_member


@pytest.mark.parametrize("name", ["apps/web/.next/server/index.js", "apps/web/node_modules/pkg/index.js",
    "apps/web/tsconfig.tsbuildinfo", ".env", "apps/web/.env.local", "deploy/.deploy.env", ".npmrc",
    ".venv/bin/python", ".git/config", "../escape", "/absolute"])
def test_local_and_generated_members_rejected(name):
    assert unsafe_member(name)


@pytest.mark.parametrize("name", [".env.example", "deploy/.env.prod.example", "brain/indexers/file_indexer.py", "LICENSE"])
def test_source_and_public_templates_allowed(name):
    assert not unsafe_member(name)


def test_wheel_and_sdist_scanned(tmp_path: Path):
    wheel = tmp_path / "bad.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("apps/web/.env.local", "test sentinel")
    with pytest.raises(ValueError, match="1 unsafe archive members"):
        check_archive(wheel)
    source = tmp_path / "bad.tar.gz"
    with tarfile.open(source, "w:gz") as archive:
        info = tarfile.TarInfo("source/apps/web/.next/cache")
        archive.addfile(info)
    with pytest.raises(ValueError, match="1 unsafe archive members"):
        check_archive(source)
