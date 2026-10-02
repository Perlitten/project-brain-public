"""Project Brain release identity helpers."""

import os
from pathlib import Path

from brain._version import (
    DASHBOARD_ASSET_VERSION,
    RELEASE_CHANNEL,
    RELEASE_CODENAME,
    __version__,
)
from brain.release_identity import read_baked_source_manifest

__all__ = [
    "DASHBOARD_ASSET_VERSION",
    "RELEASE_CHANNEL",
    "RELEASE_CODENAME",
    "__version__",
    "build_info",
    "main",
]


def build_info() -> dict[str, str]:
    manifest = read_baked_source_manifest(Path(__file__).resolve().parents[1])
    baked_build_sha = (
        str(manifest.get("source_revision") or "")
        if manifest is not None
        else ""
    )
    baked_source_digest = (
        str(manifest.get("content_digest") or "")
        if manifest is not None
        else ""
    )
    return {
        "name": "Project Brain",
        "version": __version__,
        "release_channel": RELEASE_CHANNEL,
        "release_codename": RELEASE_CODENAME,
        # The source manifest is baked into the image after COPY. Prefer it to
        # mutable runtime environment values for release identity.
        "build_sha": baked_build_sha or os.environ.get("BRAIN_BUILD_SHA", "unknown"),
        "build_time": os.environ.get("BRAIN_BUILD_TIME", "unknown"),
        "source_digest": (
            baked_source_digest
            or os.environ.get("BRAIN_SOURCE_DIGEST", "unknown")
        ),
    }


def main() -> None:
    info = build_info()
    print(f"{info['name']} {info['version']} ({info['release_codename']})")


if __name__ == "__main__":
    main()
