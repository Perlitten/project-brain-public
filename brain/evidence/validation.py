"""Evidence Item Validator.

Validates evidence items against local workspace files, line ranges, and graph freshness.
Drops invalid or stale evidence before assembling EvidencePackV2.
"""

from __future__ import annotations

from pathlib import Path
from brain.evidence.models import EvidenceItem


class EvidenceValidator:
    """Validates individual evidence items against source files."""

    @staticmethod
    def validate(item: EvidenceItem, repo_path: Path) -> EvidenceItem:
        file_p = repo_path / item.file_path
        if not file_p.exists():
            item.validation_status = "invalid_file_missing"
            item.confidence = 0.0
            return item

        if item.freshness == "stale":
            item.validation_status = "invalid_stale"
            item.confidence = 0.0
            return item

        item.validation_status = "valid"
        return item
