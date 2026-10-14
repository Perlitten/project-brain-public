import json
from zipfile import ZipFile

from scripts.check_third_party_notices import NOTICE, ROOT, archive_errors, inventory_errors


def test_notice_matches_pinned_packages_and_bundled_fonts():
    assert inventory_errors(ROOT, NOTICE.read_text(encoding="utf-8")) == []


def test_backend_builder_and_production_digest_include_canonical_notices():
    assert "COPY docs/legal/THIRD_PARTY_NOTICES.md ./docs/legal/THIRD_PARTY_NOTICES.md" in (ROOT / "Dockerfile").read_text()
    assert "--include docs/legal/THIRD_PARTY_NOTICES.md" in (ROOT / "deploy/server_up.sh").read_text()
    assert "python scripts/check_third_party_notices.py dist/*.whl dist/*.tar.gz" in (ROOT / ".github/workflows/release.yml").read_text()


def test_changed_direct_web_version_is_rejected(tmp_path):
    web = tmp_path / "apps/web"
    web.mkdir(parents=True)
    lock = json.loads((ROOT / "apps/web/package-lock.json").read_text())
    lock["packages"]["node_modules/typescript"]["version"] = "999.0.0"
    (web / "package-lock.json").write_text(json.dumps(lock))
    (tmp_path / "requirements.lock").symlink_to(ROOT / "requirements.lock")
    (tmp_path / "requirements-dev.lock").symlink_to(ROOT / "requirements-dev.lock")
    (web / "public/fonts").mkdir(parents=True)
    for name in ("OFL-JetBrainsMono.txt", "OFL-Outfit.txt"):
        (web / "public/fonts" / name).symlink_to(ROOT / "apps/web/public/fonts" / name)
    assert any("web: typescript:" in error for error in inventory_errors(tmp_path, NOTICE.read_text()))


def test_missing_npm_alias_is_rejected():
    notice = NOTICE.read_text().replace("| typescript5 | 5.9.3 | Apache-2.0 |", "")
    assert any("web: typescript5:" in error for error in inventory_errors(ROOT, notice))


def test_wheel_without_exact_notices_is_rejected(tmp_path):
    wheel = tmp_path / "synthetic.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr("brain/README.md", "safe synthetic content")
    assert archive_errors(wheel, NOTICE.read_bytes()) == [f"{wheel.name}: no packaged brain/THIRD_PARTY_NOTICES.md"]
    with ZipFile(wheel, "w") as archive:
        archive.writestr("brain/THIRD_PARTY_NOTICES.md", b"stale")
    assert archive_errors(wheel, NOTICE.read_bytes()) == [f"{wheel.name}: packaged notice differs from source"]
