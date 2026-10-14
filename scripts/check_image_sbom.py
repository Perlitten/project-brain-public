"""Require installed application dependencies in image SPDX inventories."""

import argparse
import json
from pathlib import Path
import re
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def validate_image_sbom(payload: dict, kind: str) -> None:
    packages = {
        normalized(package["name"]): package.get("versionInfo", "")
        for package in payload.get("packages", [])
    }
    if kind == "web":
        required = {"next", "react"}
    else:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
        required = set()
        for dependency in project["dependencies"]:
            name = re.match(r"[A-Za-z0-9_.-]+", dependency)
            if name is None:
                raise ValueError("Invalid distribution name in package dependencies")
            required.add(normalized(name.group()))
        required.add("project-brain")
        if packages.get("project-brain") != project["version"]:
            raise ValueError("Image SBOM is missing the matching installed Brain distribution")
    missing = sorted(name for name in required if not packages.get(name))
    if missing:
        raise ValueError(f"Image SBOM is missing installed dependencies: {', '.join(missing)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=["api", "worker", "web"], required=True)
    parser.add_argument("sbom", type=Path)
    args = parser.parse_args()
    try:
        validate_image_sbom(json.loads(args.sbom.read_text()), args.kind)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        raise SystemExit(str(exc)) from None
    print(f"PASS: installed {args.kind} dependencies are inventoried")


if __name__ == "__main__":
    main()
