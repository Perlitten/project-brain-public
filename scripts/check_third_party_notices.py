"""Check redistributed notices against the pinned Python and npm inventories."""

import json
from pathlib import Path
import re
import sys
import tarfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
NOTICE = ROOT / "docs/legal/THIRD_PARTY_NOTICES.md"
LOCK_ROW = re.compile(r"^([a-z0-9._-]+)(?:\[[^]]+\])?==([^ ]+) \\", re.MULTILINE)
NOTICE_ROW = re.compile(r"^\| ([a-z0-9@/._+-]+)(?:\[[^]]+\])? \| ([^| ]+) \| ([^|]+) \|$", re.MULTILINE)


def pinned(path: Path) -> dict[str, str]:
    return dict(LOCK_ROW.findall(path.read_text(encoding="utf-8")))


def rows(section: str) -> dict[str, tuple[str, str]]:
    return {name: (version, license_name.strip()) for name, version, license_name in NOTICE_ROW.findall(section)}


def inventory_errors(root: Path, notice: str) -> list[str]:
    errors: list[str] = []
    runtime = pinned(root / "requirements.lock")
    dev = pinned(root / "requirements-dev.lock")
    sections = [
        ("runtime", notice.split("### Runtime (`requirements.lock`)", 1)[1].split("### Development only", 1)[0], runtime),
        ("dev-only", notice.split("### Development only (`requirements-dev.lock`)", 1)[1].split("### Optional extras", 1)[0],
         {name: version for name, version in dev.items() if name not in runtime}),
    ]
    for label, section, expected in sections:
        observed = rows(section)
        for name in sorted(expected.keys() | observed.keys()):
            if name not in observed or name not in expected or observed[name][0] != expected[name] or not observed[name][1]:
                errors.append(f"{label}: {name}: lock {expected.get(name)}, notice {observed.get(name)}")

    npm = json.loads((root / "apps/web/package-lock.json").read_text(encoding="utf-8"))["packages"]
    packages = npm[""]["dependencies"] | npm[""]["devDependencies"]
    web_section = notice.split("## 3. Web UI dependencies", 1)[1].split("## 4. Container images", 1)[0]
    observed_web = rows(web_section)
    for name in sorted(packages.keys() | observed_web.keys()):
        installed = npm.get(f"node_modules/{name}", {})
        expected = (installed.get("version"), installed.get("license"))
        if name not in packages or observed_web.get(name) != expected:
            errors.append(f"web: {name}: lock {expected}, notice {observed_web.get(name)}")

    for filename in ("OFL-JetBrainsMono.txt", "OFL-Outfit.txt"):
        if not (root / "apps/web/public/fonts" / filename).is_file() or filename not in notice:
            errors.append(f"font: missing license or reference for {filename}")
    return errors


def archive_errors(path: Path, notice: bytes) -> list[str]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            name = "brain/THIRD_PARTY_NOTICES.md"
            if name not in archive.namelist():
                return [f"{path.name}: no packaged {name}"]
            included = archive.read(name)
    else:
        with tarfile.open(path) as archive:
            matched = [m for m in archive.getmembers() if m.name.endswith("/docs/legal/THIRD_PARTY_NOTICES.md")]
            if len(matched) != 1:
                return [f"{path.name}: expected exactly one source notice, found {len(matched)}"]
            stream = archive.extractfile(matched[0])
            included = stream.read() if stream else b""
            prefix = matched[0].name.removesuffix("docs/legal/THIRD_PARTY_NOTICES.md")
            for font in ("OFL-JetBrainsMono.txt", "OFL-Outfit.txt"):
                font_path = f"{prefix}apps/web/public/fonts/{font}"
                try:
                    entry = archive.getmember(font_path)
                    content = archive.extractfile(entry)
                except KeyError:
                    return [f"{path.name}: missing bundled font license {font_path}"]
                if not content or content.read() != (ROOT / "apps/web/public/fonts" / font).read_bytes():
                    return [f"{path.name}: changed bundled font license {font_path}"]
    return [] if included == notice else [f"{path.name}: packaged notice differs from source"]


def main() -> int:
    notice = NOTICE.read_bytes()
    errors = inventory_errors(ROOT, notice.decode("utf-8"))
    for name in sys.argv[1:]:
        errors.extend(archive_errors(Path(name), notice))
    for error in errors:
        print(error, file=sys.stderr)
    if not errors:
        print("PASS: pinned runtime, dev-only and direct web dependencies; bundled fonts; supplied archives")
    return bool(errors)


if __name__ == "__main__":
    raise SystemExit(main())
