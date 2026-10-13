"""Regressions for newcomer links, release state and installation contracts."""

from pathlib import Path

import pytest

from scripts.check_docs_consistency import CURRENT_PAGES, PUBLISHED, _anchors, documentation_findings


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def documentation_copy(tmp_path):
    # Copy the small local-link targets recursively, avoiding runtime data or
    # generated artifacts; independent regressions mutate documentation only.
    from scripts.check_docs_consistency import MARKDOWN_LINK, _without_code
    from urllib.parse import unquote, urlsplit

    pending = [Path(name) for name in CURRENT_PAGES]
    pending += [Path(name) for name in (
        "pyproject.toml", "brain/_version.py", "CHANGELOG.md",
        "scripts/bootstrap.sh", "scripts/start.sh", "apps/mcp_server/server.py",
    )]
    visited = set()
    while pending:
        relative = pending.pop()
        if relative in visited:
            continue
        visited.add(relative)
        original = ROOT / relative
        destination = tmp_path / relative
        if original.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        if not original.is_file():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(original.read_bytes())
        if original.suffix == ".md":
            for raw in MARKDOWN_LINK.findall(_without_code(original.read_text())):
                link = urlsplit(raw)
                if link.scheme or link.netloc or not link.path:
                    continue
                target = (original.parent / unquote(link.path)).resolve()
                if target.is_relative_to(ROOT):
                    pending.append(target.relative_to(ROOT))
    assert documentation_findings(tmp_path) == []
    return tmp_path


def test_heading_anchors_ignore_code_blocks_and_handle_duplicates():
    assert _anchors("# Overview\n## The `API`: [guide](README.md)\n## Overview\n```md\n# Fake\n```\n") == {
        "overview", "the-api-guide", "overview-1",
    }


def test_detects_removed_local_navigation_target(documentation_copy):
    (documentation_copy / "docs/MCP.md").unlink()
    assert any("missing local link target: docs/MCP.md" in issue
               for issue in documentation_findings(documentation_copy))


def test_detects_broken_quickstart_anchor(documentation_copy):
    page = documentation_copy / "README.md"
    page.write_text(page.read_text().replace("## Quickstart", "## Installation"))
    assert any("missing Markdown anchor: #quickstart" in issue
               for issue in documentation_findings(documentation_copy))


def test_detects_stale_published_version_in_faq(documentation_copy):
    page = documentation_copy / "docs/FAQ.md"
    text = page.read_text()
    published = PUBLISHED.search(text).group(1)
    page.write_text(text.replace(f"v{published}", "v0.0.0"))
    assert "current guides disagree on latest published release" in documentation_findings(documentation_copy)


def test_detects_clone_and_cd_directory_disagreement(documentation_copy):
    page = documentation_copy / "docs/GETTING_STARTED.md"
    page.write_text(page.read_text().replace("cd project-brain", "cd unrelated-directory"))
    assert any("recommended clone must pin published tag and matching directory" in issue
               for issue in documentation_findings(documentation_copy))


def test_source_candidate_can_be_newer_than_published_release(documentation_copy):
    import tomllib

    source = tomllib.loads((documentation_copy / "pyproject.toml").read_text())["project"]["version"]
    major, minor, patch = map(int, source.split("."))
    candidate = f"{major}.{minor}.{patch + 1}"
    for name in ("pyproject.toml", "brain/_version.py"):
        page = documentation_copy / name
        page.write_text(page.read_text().replace(source, candidate))
    page = documentation_copy / "CHANGELOG.md"
    page.write_text(page.read_text().replace("## [Unreleased]", f"## [Unreleased]\n\n## [{candidate}]", 1))
    assert documentation_findings(documentation_copy) == []
