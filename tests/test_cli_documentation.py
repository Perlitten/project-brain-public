"""User-facing command discovery and target-relative artifact contracts."""

from pathlib import Path

from typer.main import get_command
from typer.testing import CliRunner

from apps.cli.main import cli
from brain.config.settings import Settings, settings
from scripts.check_docs_consistency import CURRENT_PAGES, documentation_findings


ROOT = Path(__file__).resolve().parents[1]


def test_current_docs_include_operational_entry_points():
    required = {"docs/EVAL.md", "docs/CLI.md", "docs/DASHBOARD.md", "docs/runbooks/backup-restore.md"}
    assert required.issubset(CURRENT_PAGES)
    assert documentation_findings(ROOT) == []


def test_cli_reference_matches_installed_command_tree():
    text = (ROOT / "docs/CLI.md").read_text()
    missing = [name for name in get_command(cli).commands if f"`brain {name}`" not in text]
    assert not missing, f"Undocumented CLI commands: {missing}"
    runner = CliRunner()
    for name in get_command(cli).commands:
        result = runner.invoke(cli, [name, "--help"])
        assert result.exit_code == 0, result.output
    assert "<target-repository>/reports/initial-audit.md" in text
    assert "not a `brain backup` command" in text


def test_audit_writes_to_distinct_target_not_brain_checkout(tmp_path, monkeypatch):
    target = tmp_path / "synthetic-target"
    target.mkdir()
    (target / "example.py").write_text("def example():\n    return 42\n")
    monkeypatch.setattr(settings, "REPO_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "REPORT_OUTPUT_DIR", None)
    result = CliRunner().invoke(cli, ["audit", "--repo", str(target)])
    assert result.exit_code == 0, result.output
    report = target / "reports/initial-audit.md"
    assert report.is_file()
    assert report.resolve().as_posix() in result.output
    assert "<target-repository>/reports/initial-audit.md" in (ROOT / "docs/CLI.md").read_text()


def test_faq_distinguishes_published_release_from_current_defaults():
    text = (ROOT / "docs/FAQ.md").read_text()
    assert "unreleased `main`" in text
    assert "v0.9.2" in text
    for field in ("BRAIN_AGENT_CONTEXT_V2_MODE", "BRAIN_STALE_CONTEXT_POLICY"):
        default = Settings.model_fields[field].default
        assert f"{field}={default}" in text
    changelog = (ROOT / "CHANGELOG.md").read_text().split("## [0.9.2]", 1)[0]
    for change in ("Lite", "warn_and_serve", "RETRIEVAL_V2_ENABLED", "summaries"):
        assert change in changelog
