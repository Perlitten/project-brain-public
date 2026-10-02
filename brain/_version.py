from pathlib import Path

__version__ = "0.9.0"
VERSION = "0.9.0"
RELEASE_CHANNEL = "self-hosted"
RELEASE_CODENAME = "tournament-sandbox-v0.9.0"


def _static_fingerprint() -> str:
    """Cache-busting token derived from the dashboard assets themselves.

    This used to be a hand-written string ("20260707-proactive"). It was last
    bumped on 7 July, so every stylesheet change after that date was served to
    returning visitors from their browser cache — a redesign could ship to the
    server and be invisible to the only person looking at it. Deriving it from
    the newest asset mtime removes the step someone has to remember.

    Falls back to the release codename if the directory cannot be read, so a
    packaging layout without static files still boots.
    """
    static_dir = Path(__file__).resolve().parents[1] / "apps" / "api" / "static"
    try:
        newest = max(
            (p.stat().st_mtime_ns for p in static_dir.rglob("*") if p.is_file()),
            default=0,
        )
    except OSError:
        return RELEASE_CODENAME
    return f"{newest // 1_000_000_000}" if newest else RELEASE_CODENAME


DASHBOARD_ASSET_VERSION = _static_fingerprint()
