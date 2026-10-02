"""Experiment package generation — Phase E6.

Writes a self-describing directory for one experiment:

    reports/change-experiments/<experiment-id>/
      manifest.json
      manifest.sha256          (external — never covers itself)
      experiment.json
      comparison.json
      recommendation.json
      option-<id>/
        patch.diff
        validation.json
        architecture-before.json
        architecture-after.json
        impact.json
        stdout.log
        stderr.log
      summary.md

Every artifact is checksummed in the manifest, and the manifest's own checksum
lives outside it so tampering with the manifest cannot go unnoticed. Logs are
redacted before they are written.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from brain.experiments.models import ComparisonResult, Experiment, ExperimentOption, utc_now
from brain.lab.engine import ValidationRunner

MANIFEST_NAME = "manifest.json"
MANIFEST_CHECKSUM_NAME = "manifest.sha256"

NON_AUTONOMY_STATEMENT = (
    "Project Brain may apply candidate patches only inside disposable managed "
    "workspaces for validation. It does not apply patches to authoritative "
    "repositories, commit changes, push branches, merge pull requests, or "
    "deploy software."
)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write(path: Path, text: str) -> str:
    """Write durably and return the artifact's checksum."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)
    return _sha256_bytes(data)


def _json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, indent=2, default=str) + "\n"


def _redact(text: str) -> str:
    return ValidationRunner._redact(text or "")


def _option_streams(option: ExperimentOption) -> tuple[str, str]:
    """Concatenate the option's captured stdout/stderr, redacted."""
    results = ((option.results or {}).get("validation") or {}).get("results", [])
    out, err = [], []
    for entry in results:
        argv = entry.get("argv")
        out.append(f"$ {argv}\n{entry.get('stdout_snippet', '')}\n")
        err.append(f"$ {argv}\n{entry.get('stderr_snippet', '')}\n")
    return _redact("".join(out)), _redact("".join(err))


class ExperimentPackager:
    """Materializes an auditable package for one experiment."""

    def __init__(self, reports_root: Path):
        self.reports_root = Path(reports_root)

    def package_dir(self, experiment_id: str) -> Path:
        return self.reports_root / experiment_id

    def build(
        self,
        experiment: Experiment,
        comparison: Optional[ComparisonResult] = None,
        recommendation: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        root = self.package_dir(experiment.experiment_id)
        root.mkdir(parents=True, exist_ok=True)
        checksums: Dict[str, str] = {}

        def emit(relative: str, text: str) -> None:
            checksums[relative] = _write(root / relative, text)

        emit("experiment.json", _json(experiment.to_dict()))
        emit("comparison.json", _json(comparison.to_dict() if comparison else {}))
        emit("recommendation.json", _json(recommendation or {}))

        for option in experiment.options:
            folder = f"option-{option.option_id}"
            session = option.results or {}
            stdout_text, stderr_text = _option_streams(option)
            emit(f"{folder}/patch.diff", option.candidate_patch)
            emit(f"{folder}/validation.json", _json(session.get("validation") or {}))
            emit(
                f"{folder}/architecture-before.json",
                _json(
                    {
                        "graph_generation": experiment.graph_generation,
                        "base_revision": experiment.base_revision,
                        "policy_version": experiment.policy_version,
                    }
                ),
            )
            emit(
                f"{folder}/architecture-after.json",
                _json(
                    {
                        "cycle_delta": option.cycle_delta,
                        "forbidden_edge_delta": option.forbidden_edge_delta,
                        "coupling_delta": option.coupling_delta,
                        "hotspot_delta": option.hotspot_delta,
                        "new_findings": option.new_findings,
                        "new_critical_findings": option.new_critical_findings,
                        "resolved_findings": option.resolved_findings,
                        "architecture_improved": option.architecture_improved,
                    }
                ),
            )
            emit(
                f"{folder}/impact.json",
                _json(
                    {
                        "blast_radius": option.impact_blast_radius,
                        "changed_files": option.changed_files,
                        "changed_lines": option.changed_lines,
                        "post_patch": session.get("post_patch") or {},
                    }
                ),
            )
            emit(f"{folder}/stdout.log", stdout_text)
            emit(f"{folder}/stderr.log", stderr_text)

        emit("summary.md", self._summary(experiment, comparison))

        manifest = {
            "schema_version": 1,
            "experiment_id": experiment.experiment_id,
            "repository_id": experiment.repository_id,
            "base_revision": experiment.base_revision,
            "graph_generation": experiment.graph_generation,
            "policy_version": experiment.policy_version,
            "state": experiment.state,
            "conclusion": experiment.conclusion,
            "recommended_option_id": experiment.recommended_option_id,
            "semantic_fingerprint": experiment.semantic_fingerprint,
            "generated_at_utc": utc_now(),
            "non_autonomy_statement": NON_AUTONOMY_STATEMENT,
            "artifact_checksums": dict(sorted(checksums.items())),
        }
        manifest_text = _json(manifest)
        manifest_digest = _write(root / MANIFEST_NAME, manifest_text)
        # The manifest checksum stays outside the manifest: a file cannot
        # honestly attest to its own contents.
        _write(root / MANIFEST_CHECKSUM_NAME, f"{manifest_digest}  {MANIFEST_NAME}\n")
        return manifest

    @staticmethod
    def verify(package_dir: Path) -> Dict[str, Any]:
        """Re-check every artifact against the manifest, and the manifest
        against its external checksum."""
        root = Path(package_dir)
        issues: List[str] = []
        manifest_path = root / MANIFEST_NAME
        checksum_path = root / MANIFEST_CHECKSUM_NAME

        if not manifest_path.is_file():
            return {"valid": False, "issues": ["manifest.json is missing"]}
        if not checksum_path.is_file():
            return {"valid": False, "issues": ["manifest.sha256 is missing"]}

        manifest_bytes = manifest_path.read_bytes()
        expected = checksum_path.read_text(encoding="utf-8").split()[0].strip()
        if _sha256_bytes(manifest_bytes) != expected:
            issues.append("manifest.json does not match manifest.sha256")

        try:
            manifest = json.loads(manifest_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return {"valid": False, "issues": issues + [f"manifest.json unreadable: {exc}"]}

        for relative, digest in (manifest.get("artifact_checksums") or {}).items():
            artifact = root / relative
            if not artifact.is_file():
                issues.append(f"missing artifact: {relative}")
                continue
            if _sha256_bytes(artifact.read_bytes()) != digest:
                issues.append(f"checksum mismatch: {relative}")

        return {"valid": not issues, "issues": issues, "manifest": manifest}

    @staticmethod
    def _summary(experiment: Experiment, comparison: Optional[ComparisonResult]) -> str:
        lines = [
            f"# Experiment {experiment.experiment_id}",
            "",
            f"- Repository: `{experiment.repository_id}`",
            f"- Base revision: `{experiment.base_revision}`",
            f"- Graph generation: `{experiment.graph_generation or 'n/a'}`",
            f"- State: **{experiment.state}**",
            f"- Conclusion: **{experiment.conclusion or 'n/a'}**",
            f"- Recommended option: `{experiment.recommended_option_id or 'none'}`",
            "",
            "> A recommendation is not an approval.",
            f"> {NON_AUTONOMY_STATEMENT}",
            "",
            "## Options",
            "",
            "| Option | Executed | Required tests | Score | Disqualified | Reason |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        scores = comparison.scores if comparison else {}
        for option in experiment.options:
            score = scores.get(option.option_id)
            lines.append(
                f"| `{option.option_id}` | {option.executed} | "
                f"{'pass' if option.passed_required else 'fail'} | "
                f"{score if score is not None else '—'} | {option.disqualified} | "
                f"{option.disqualification_reason or '—'} |"
            )

        if comparison and comparison.warnings:
            lines += ["", "## Warnings", ""]
            lines += [f"- {w}" for w in comparison.warnings]
        if comparison and comparison.ties:
            lines += ["", "## Ties", ""]
            lines += [f"- {', '.join(t)}" for t in comparison.ties]
        if comparison and comparison.formula:
            lines += ["", "## Ranking formula", "", f"`{comparison.formula}`"]
        lines.append("")
        return "\n".join(lines)
