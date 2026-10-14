"""Validate newcomer documentation links and published/source release contracts.

Offline and safe to run in CI: external reachability belongs in release
verification, while this catches stale versions, broken local navigation and
installation commands pointing at unavailable entry points.
"""

from __future__ import annotations

from pathlib import Path
import re
import sys
import tomllib
from urllib.parse import unquote, urlsplit


CURRENT_PAGES = (
    "README.md", ".github/CONTRIBUTING.md", "docs/README.md", "docs/FAQ.md",
    "docs/GETTING_STARTED.md", "docs/ARCHITECTURE.md", "docs/MODEL_PROVIDERS.md",
    "docs/MCP.md", "docs/DEMO.md", "docs/historical/README.md", "docs/release/README.md",
    "docs/release/FINAL_PRODUCTIZATION_REPORT.md",
    ".github/CODE_OF_CONDUCT.md", "SECURITY.md", "docs/ROADMAP.md",
    "docs/legal/THIRD_PARTY_NOTICES.md", "docs/operations/DEPLOYMENT.md",
    "docs/operations/AGENT_DEPLOYMENT.md", "deploy/RUNBOOK.md", "examples/README.md",
    "docs/EVAL.md", "docs/CLI.md", "docs/DASHBOARD.md", "docs/runbooks/backup-restore.md",
)
PUBLISHED_PAGES = ("README.md", "docs/FAQ.md", "docs/GETTING_STARTED.md")
PUBLISHED = re.compile(r"Latest published release: \*\*\[v(\d+\.\d+\.\d+)\]\(([^)]+)\)\*\*")
REPOSITORY = "https://github.com/Perlitten/project-brain-public"
MARKDOWN_LINK = re.compile(r"!?\[[^\]\n]*\]\(([^)\s]+)(?:\s+[^)]*)?\)")


def _without_code(text: str) -> str:
    return re.sub(r"```[^\n]*\n.*?```", "", text, flags=re.DOTALL)


def _anchors(text: str) -> set[str]:
    anchors = set(re.findall(r'<a\s+[^>]*id=["\']([^"\']+)', text))
    counts: dict[str, int] = {}
    for heading in re.findall(r"^#{1,6}\s+(.+?)\s*#*\s*$", _without_code(text), re.MULTILINE):
        heading = re.sub(r"!?\[([^]]+)\]\([^)]+\)", r"\1", heading)
        slug = re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
        count = counts.get(slug, 0)
        counts[slug] = count + 1
        anchors.add(f"{slug}-{count}" if count else slug)
    return anchors


def documentation_findings(root: Path, pages: tuple[str, ...] = CURRENT_PAGES) -> list[str]:
    findings: list[str] = []
    texts: dict[str, str] = {}
    for name in pages:
        path = root / name
        if not path.is_file():
            findings.append(f"{name}: missing current documentation page")
            continue
        texts[name] = path.read_text(encoding="utf-8")
        for raw in MARKDOWN_LINK.findall(_without_code(texts[name])):
            link = urlsplit(raw.strip("<>"))
            if link.scheme or link.netloc:
                continue
            destination = unquote(link.path)
            target = (path.parent / destination).resolve() if destination else path.resolve()
            if not target.is_relative_to(root.resolve()):
                findings.append(f"{name}: local link escapes repository: {raw}")
            elif not target.exists():
                findings.append(f"{name}: missing local link target: {raw}")
            elif link.fragment and target.is_file() and target.suffix == ".md":
                if unquote(link.fragment) not in _anchors(target.read_text(encoding="utf-8")):
                    findings.append(f"{name}: missing Markdown anchor: {raw}")
    if not set(PUBLISHED_PAGES).issubset(texts):
        return findings
    versions = set()
    for name in PUBLISHED_PAGES:
        match = PUBLISHED.search(texts[name])
        if not match:
            findings.append(f"{name}: missing explicit latest published release")
            continue
        version, release_url = match.groups()
        versions.add(version)
        if release_url != f"{REPOSITORY}/releases/tag/v{version}":
            findings.append(f"{name}: published version disagrees with release URL")
    if len(versions) != 1:
        findings.append("current guides disagree on latest published release")
        return findings
    published = versions.pop()
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    source = project["version"]
    runtime = re.search(r'__version__\s*=\s*["\']([^"\']+)', (root / "brain/_version.py").read_text())
    if not runtime or runtime.group(1) != source:
        findings.append("package metadata and runtime versions disagree")
    if tuple(map(int, published.split("."))) > tuple(map(int, source.split("."))):
        findings.append("published version is newer than source version")
    changelog = (root / "CHANGELOG.md").read_text()
    if f"## [{source}]" not in changelog:
        findings.append("source version has no changelog section")
    if f"[{published}]: {REPOSITORY}/releases/tag/v{published}" not in changelog:
        findings.append("published changelog link does not point to its release")
    if f"[Unreleased]: {REPOSITORY}/compare/v{published}...main" not in changelog:
        findings.append("unreleased comparison does not start at latest published release")
    clone = f"git clone --branch v{published} {REPOSITORY}.git project-brain\ncd project-brain"
    for name in ("README.md", "docs/GETTING_STARTED.md"):
        if clone not in texts[name]:
            findings.append(f"{name}: recommended clone must pin published tag and matching directory")
        if "./scripts/bootstrap.sh" not in texts[name] or "./scripts/start.sh" not in texts[name]:
            findings.append(f"{name}: recommended source installation is incomplete")
    for entry in ("scripts/bootstrap.sh", "scripts/start.sh", "apps/mcp_server/server.py"):
        if not (root / entry).is_file():
            findings.append(f"advertised installation/MCP entry point missing: {entry}")
    mcp = (root / "apps/mcp_server/server.py").read_text()
    if '"search_code"' not in mcp or '"prepare_task_context"' not in mcp:
        findings.append("first MCP task tools are missing from the local server")
    return findings


def main() -> int:
    findings = documentation_findings(Path(__file__).resolve().parents[1])
    if findings:
        print("\n".join(findings), file=sys.stderr)
        return 1
    print("Current documentation links, published versions and installation contracts passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
