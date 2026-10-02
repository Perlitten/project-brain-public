"""Architecture Policy Loader and Schema Validator for Architectural Change Guard (Phase 4)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Set

import yaml


@dataclass
class EnforcementConfig:
    mode: str = "fail"  # 'report', 'warn', 'fail'
    fail_on_states: Set[str] = field(default_factory=lambda: {"new"})
    fail_on_severities: Set[str] = field(default_factory=lambda: {"critical"})
    max_new_warnings: int = 3
    allow_preexisting: bool = True


@dataclass
class ScanConfig:
    dependency_expansion: bool = True
    max_dependency_files: int = 50
    max_files: int = 5000
    max_file_size_bytes: int = 2097152


@dataclass
class ArchitecturePolicy:
    version: int = 1
    enforcement: EnforcementConfig = field(default_factory=EnforcementConfig)
    rule_overrides: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    scan: ScanConfig = field(default_factory=ScanConfig)
    waiver_file: str = ".brain/drift-waivers.yaml"
    max_waiver_duration_days: int = 90

    @classmethod
    def default_policy(cls) -> ArchitecturePolicy:
        return cls()

    @classmethod
    def load_from_yaml(cls, yaml_path: Path) -> ArchitecturePolicy:
        if not yaml_path.exists():
            return cls.default_policy()

        try:
            with open(yaml_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception as exc:
            raise ValueError(f"Malformed architecture policy YAML in {yaml_path}: {exc}")

        if not isinstance(data, dict):
            raise ValueError(f"Invalid architecture policy schema in {yaml_path}: Root must be a dict")

        version = data.get("version", 1)
        if version != 1:
            raise ValueError(f"Unsupported architecture policy version: {version}")

        enf_raw = data.get("enforcement") or {}
        mode = str(enf_raw.get("mode", "fail")).lower()
        if mode not in {"report", "warn", "fail"}:
            raise ValueError(f"Invalid enforcement mode: {mode}")

        fail_on_raw = enf_raw.get("fail_on") or {}
        states = set(fail_on_raw.get("states") or ["new"])
        valid_states = {"new", "persistent", "moved", "resolved"}
        if not states.issubset(valid_states):
            raise ValueError(f"Invalid fail_on.states: {states - valid_states}")

        severities = set(fail_on_raw.get("severities") or ["critical"])
        valid_severities = {"info", "warning", "critical"}
        if not severities.issubset(valid_severities):
            raise ValueError(f"Invalid fail_on.severities: {severities - valid_severities}")

        max_warnings = int(enf_raw.get("max_new_warnings", 3))
        if max_warnings < 0:
            raise ValueError("max_new_warnings cannot be negative")

        enforcement = EnforcementConfig(
            mode=mode,
            fail_on_states=states,
            fail_on_severities=severities,
            max_new_warnings=max_warnings,
            allow_preexisting=bool(enf_raw.get("allow_preexisting", True)),
        )

        scan_raw = data.get("scan") or {}
        scan = ScanConfig(
            dependency_expansion=bool(scan_raw.get("dependency_expansion", True)),
            max_dependency_files=int(scan_raw.get("max_dependency_files", 50)),
            max_files=int(scan_raw.get("max_files", 5000)),
            max_file_size_bytes=int(scan_raw.get("max_file_size_bytes", 2097152)),
        )

        waivers_raw = data.get("waivers") or {}
        waiver_file = str(waivers_raw.get("file", ".brain/drift-waivers.yaml"))
        max_duration = int(waivers_raw.get("max_duration_days", 90))

        rule_overrides = data.get("rules") or {}

        return cls(
            version=version,
            enforcement=enforcement,
            rule_overrides=rule_overrides,
            scan=scan,
            waiver_file=waiver_file,
            max_waiver_duration_days=max_duration,
        )
