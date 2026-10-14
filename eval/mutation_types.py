"""Bind mypy diagnostics to frozen mutant bodies without crediting harness errors."""

from __future__ import annotations

import bisect
import gzip
import hashlib
import json
import re
from collections import defaultdict
from importlib.metadata import version
from pathlib import Path


def build_type_proof(inventory: dict, generated: Path, diagnostics: Path) -> dict:
    errors: dict[str, list[dict]] = defaultdict(list)
    for line in diagnostics.read_text(encoding="utf-8").splitlines():
        diagnostic = json.loads(line)
        if diagnostic["severity"] == "error":
            path = Path(diagnostic["file"])
            if path.is_absolute() and path.is_relative_to(generated.resolve()):
                path = path.relative_to(generated.resolve())
            errors[path.as_posix()].append(diagnostic)
    modules = []
    for item in inventory["modules"]:
        module = item["module"]
        source = generated / module
        if hashlib.sha256(source.read_bytes()).hexdigest() != item["generated_sha256"]:
            raise ValueError(f"Generated source changed: {module}")
        meta = json.loads((generated / (module + ".meta")).read_text(encoding="utf-8"))
        spans = json.loads((generated / (module + ".spans")).read_text(encoding="utf-8"))["spans"]
        keys = {key.rpartition(".")[2]: key for key in meta["exit_code_by_key"]}
        ordered = sorted((start, end, name) for name, (start, end) in spans.items())
        starts = [start for start, _, _ in ordered]
        invalid: dict[str, list[dict]] = defaultdict(list)
        unmapped = []
        artifacts = []
        source_lines = None
        for diagnostic in errors.get(module, []):
            detail = {key: diagnostic[key] for key in ("line", "column", "code", "message")}
            if diagnostic["code"] == "no-redef":
                # Constructor cloning repeats annotated self attributes. This
                # error is not introduced by the candidate's behavior change.
                previous = re.search(r'Attribute ".+" already defined on line (\d+)$', diagnostic["message"])
                if previous:
                    if source_lines is None:
                        source_lines = source.read_text(encoding="utf-8").splitlines()
                    first = source_lines[int(previous[1]) - 1].split("=", 1)[0].strip()
                    current = source_lines[diagnostic["line"] - 1].split("=", 1)[0].strip()
                    if first == current:
                        artifacts.append(detail)
                        continue
            index = bisect.bisect_right(starts, diagnostic["line"]) - 1
            name = ordered[index][2] if index >= 0 and diagnostic["line"] <= ordered[index][1] else None
            if name in keys:
                invalid[keys[name]].append(detail)
            else:
                unmapped.append(detail)
        modules.append({"module": module, "sha256": item["sha256"],
                        "generated_sha256": item["generated_sha256"], "eligible": not unmapped,
                        "unmapped_errors": unmapped, "ignored_clone_diagnostics": artifacts,
                        "invalid_candidates": dict(invalid) if not unmapped else {},
                        "unclassified_candidate_errors": len(invalid) if unmapped else 0})
    paths = {item["module"] for item in inventory["modules"]}
    return {"schema": 1, "base": inventory["base"], "mutmut": "3.8.0", "mypy": version("mypy"),
            "command": "python -m mypy --explicit-package-bases --follow-imports=silent --output=json brain apps scripts",
            "diagnostics_sha256": hashlib.sha256(diagnostics.read_bytes()).hexdigest(), "modules": modules,
            "outside_inventory_errors": {path: rows for path, rows in errors.items() if path not in paths}}


def read_type_exclusions(path: Path, modules: list[dict]) -> dict[str, list[dict]]:
    proof = json.loads(gzip.decompress(path.read_bytes()))
    if proof["schema"] != 1 or proof["mutmut"] != "3.8.0" or proof["outside_inventory_errors"]:
        raise ValueError("Invalid or incomplete type-proof controls")
    by_path = {item["module"]: item for item in proof["modules"]}
    if len(by_path) != len(proof["modules"]):
        raise ValueError("Duplicate module in type proof")
    invalid = {}
    for item in modules:
        row = by_path[item["module"]]
        if (row["sha256"] != item["sha256"] or row["generated_sha256"] != item["generated_sha256"]):
            raise ValueError(f"Type proof does not match source: {item['module']}")
        if row["eligible"]:
            if row["unmapped_errors"]:
                raise ValueError("Type exclusions cannot ignore unmapped errors")
            for key, diagnostics in row["invalid_candidates"].items():
                if not diagnostics:
                    raise ValueError(f"Missing diagnostic for type exclusion: {key}")
                invalid[key] = diagnostics
    return invalid
