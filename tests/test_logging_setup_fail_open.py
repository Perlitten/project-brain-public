"""The file log sink must be fail-open.

The 2026-07-30 incident: /app/reports was a bind mount owned by the host user,
the container ran as 'brain', and the unconditional logger.add() crash-looped
the API before it could serve /health. The sink now degrades to stderr-only
logging instead of blocking startup.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apps.api.logging_setup import configure_file_log_sink  # noqa: E402
from loguru import logger  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_logger():
    # Detach every sink this test attaches; keep other tests' output intact.
    yield
    logger.remove()
    logger.add(sys.stderr)


def test_unwritable_reports_dir_disables_the_sink_not_the_app(tmp_path: Path):
    blocker = tmp_path / "reports-as-a-file"
    blocker.write_text("not a directory", encoding="utf-8")

    attached = configure_file_log_sink(lambda: blocker)

    assert attached is False  # and, crucially, no exception escaped


def test_writable_reports_dir_attaches_and_writes(tmp_path: Path):
    target = tmp_path / "reports"

    attached = configure_file_log_sink(lambda: target)
    assert attached is True

    # The sink filters on the record's module name (brain*/apps*); this test
    # module is neither, so impersonate one for the probe record.
    probe = logger.patch(lambda r: r.update(name="brain.probe"))
    probe.info("brain log write probe")
    logger.complete()

    log_file = target / "brain.log"
    assert log_file.exists()
    assert "brain log write probe" in log_file.read_text(encoding="utf-8")
