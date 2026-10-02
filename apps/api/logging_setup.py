"""File log sink configuration for the API.

Logs always go to stderr (loguru's default sink), which Docker collects. The
file sink under reports/ additionally feeds the dashboard's Logs page and the
/logs API. It is fail-open by design: a reports/ directory the process cannot
write to must degrade to stderr-only logging, never block startup — the
2026-07-30 incident crash-looped brain-api on exactly this (bind-mount owned
by the host user, container running as 'brain'). Deterministic ownership of
the mount itself is deploy/server_up.sh's job; this module only guarantees
that getting it wrong is a warning, not an outage.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from loguru import logger

if TYPE_CHECKING:
    from loguru import Record

LOG_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
    "{name}:{function}:{line} - {message}"
)


def _brain_records_only(record: "Record") -> bool:
    return any(term in str(record.get("name") or "") for term in ("brain", "apps"))


def configure_file_log_sink(
    reports_dir_provider: Optional[Callable[[], Path]] = None,
) -> bool:
    """Attach the brain.log file sink; return True if it was attached."""
    if reports_dir_provider is None:
        from brain.config.paths import reports_dir

        reports_dir_provider = reports_dir

    try:
        target = reports_dir_provider()
        target.mkdir(parents=True, exist_ok=True)
        logger.add(
            str(target / "brain.log"),
            rotation="10 MB",
            retention="1 day",
            format=LOG_FORMAT,
            filter=_brain_records_only,
        )
        return True
    except OSError as exc:
        logger.warning(
            "reports dir is not writable ({}: {}); file log sink disabled, "
            "stderr logging continues",
            type(exc).__name__,
            exc,
        )
        return False
