"""Review Package Generator and Manifest Writer for Workstream D."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List


@dataclass
class ReviewPackageManifest:
    package_version: str
    run_id: str
    repository: str
    base_revision: str
    candidate_revision: str
    created_at_utc: str
    run_fingerprint: str
    checksums: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ReviewPackageWriter:
    """Atomic reviewer package artifact builder."""

    @classmethod
    def create_package(
        cls,
        output_dir: Path,
        repository: str,
        base_rev: str,
        cand_rev: str,
        result_dict: dict[str, Any],
        risk_dict: dict[str, Any],
        comment_md: str,
        sarif_dict: dict[str, Any],
        annotations_list: List[dict[str, Any]],
    ) -> ReviewPackageManifest:
        output_dir = output_dir.resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        # Generate run fingerprint
        fp_str = f"{repository}:{base_rev}:{cand_rev}"
        run_fp = hashlib.sha256(fp_str.encode()).hexdigest()[:16]
        run_id = f"rev-{run_fp}"

        # Write child artifacts
        f_map = {
            "result.json": json.dumps(result_dict, indent=2),
            "risk.json": json.dumps(risk_dict, indent=2),
            "comment.md": comment_md,
            "result.sarif": json.dumps(sarif_dict, indent=2),
            "annotations.json": json.dumps(annotations_list, indent=2),
        }

        checksums: Dict[str, str] = {}
        for fname, content in f_map.items():
            file_path = output_dir / fname
            file_path.write_text(content, encoding="utf-8")
            checksums[fname] = hashlib.sha256(content.encode("utf-8")).hexdigest()

        manifest = ReviewPackageManifest(
            package_version="1.0.0",
            run_id=run_id,
            repository=repository,
            base_revision=base_rev,
            candidate_revision=cand_rev,
            created_at_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            run_fingerprint=run_fp,
            checksums=checksums,
        )

        (output_dir / "manifest.json").write_text(json.dumps(manifest.to_dict(), indent=2), encoding="utf-8")
        return manifest
