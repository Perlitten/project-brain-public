"""Remediation Plan Package Artifact Generator."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from brain.insights.remediation_models import RemediationPlan


@dataclass
class RemediationPackageManifest:
    plan_id: str
    repository: str
    source_revision: str
    created_at_utc: str
    checksums: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RemediationPackageWriter:
    """Writes a complete remediation plan package to disk."""

    @staticmethod
    def create_package(output_dir: Path, plan: RemediationPlan) -> RemediationPackageManifest:
        output_dir.mkdir(parents=True, exist_ok=True)
        now_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        # 1. plan.json
        plan_bytes = json.dumps(plan.to_dict(), indent=2).encode("utf-8")
        (output_dir / "plan.json").write_bytes(plan_bytes)

        # 2. evidence.json
        evidence_bytes = json.dumps(plan.evidence, indent=2).encode("utf-8")
        (output_dir / "evidence.json").write_bytes(evidence_bytes)

        # 3. options.json
        options_bytes = json.dumps([o.to_dict() for o in plan.remediation_options], indent=2).encode("utf-8")
        (output_dir / "options.json").write_bytes(options_bytes)

        # 4. validation.json
        val_bytes = json.dumps({"steps": plan.validation_steps, "tests": plan.tests_to_run}, indent=2).encode("utf-8")
        (output_dir / "validation.json").write_bytes(val_bytes)

        # 5. patch.diff (optional)
        checksums: dict[str, str] = {
            "plan.json": hashlib.sha256(plan_bytes).hexdigest(),
            "evidence.json": hashlib.sha256(evidence_bytes).hexdigest(),
            "options.json": hashlib.sha256(options_bytes).hexdigest(),
            "validation.json": hashlib.sha256(val_bytes).hexdigest(),
        }

        if plan.patch_diff:
            diff_bytes = plan.patch_diff.encode("utf-8")
            (output_dir / "patch.diff").write_bytes(diff_bytes)
            checksums["patch.diff"] = hashlib.sha256(diff_bytes).hexdigest()

        # 6. summary.md
        summary_md = (
            f"# Remediation Plan: {plan.plan_id}\n\n"
            f"* **Rule**: `{plan.finding_ids}`\n"
            f"* **State**: `{plan.state}`\n"
            f"* **Problem**: {plan.problem_statement}\n\n"
            f"## Recommended Option\n"
            f"{plan.remediation_options[plan.recommended_option_index].title if plan.remediation_options else 'N/A'}\n"
        )
        summary_bytes = summary_md.encode("utf-8")
        (output_dir / "summary.md").write_bytes(summary_bytes)
        checksums["summary.md"] = hashlib.sha256(summary_bytes).hexdigest()

        # 7. manifest.json
        manifest = RemediationPackageManifest(
            plan_id=plan.plan_id,
            repository=plan.repository,
            source_revision=plan.source_revision,
            created_at_utc=now_str,
            checksums=checksums,
        )
        (output_dir / "manifest.json").write_text(json.dumps(manifest.to_dict(), indent=2), encoding="utf-8")

        return manifest
