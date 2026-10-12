#!/usr/bin/env python3
"""Refresh the static coverage badge in README.md from coverage XML.

Usage: pytest --cov=brain --cov=apps --cov-report=xml:coverage.xml
       python scripts/update_coverage_badge.py coverage.xml

The badge is a static shields.io URL so no external service or token is
needed; the number is refreshed by CI (or by hand before a release).
"""
from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

README = Path(__file__).resolve().parents[1] / "README.md"


def main(xml_path: str) -> int:
    root = ET.parse(xml_path).getroot()
    rate = float(root.attrib["line-rate"]) * 100
    pct = round(rate)
    color = "brightgreen" if pct >= 70 else "yellow" if pct >= 50 else "red"
    badge = f"https://img.shields.io/badge/coverage-{pct}%25-{color}"
    text = README.read_text()
    new = re.sub(r"https://img\.shields\.io/badge/coverage-\d+%25-\w+", badge, text, count=1)
    if new == text and "img.shields.io/badge/coverage" not in text:
        print("no coverage badge marker found in README.md", file=sys.stderr)
        return 1
    README.write_text(new)
    print(f"coverage badge -> {pct}% ({color})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "coverage.xml"))
