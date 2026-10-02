"""Subsystem Definition Config Parser and Validator (.brain/subsystems.yaml)."""

from __future__ import annotations

import fnmatch
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


@dataclass
class SubsystemSpec:
    name: str
    include: List[str]
    exclude: List[str] = field(default_factory=list)
    owners: List[str] = field(default_factory=list)
    allowed_dependencies: List[str] = field(default_factory=list)
    forbidden_dependencies: List[str] = field(default_factory=list)
    public_modules: List[str] = field(default_factory=list)

    def matches_file(self, rel_path: str) -> bool:
        norm = rel_path.lstrip("./")
        # Check excludes first
        for ex in self.exclude:
            if fnmatch.fnmatch(norm, ex):
                return False
        for inc in self.include:
            if fnmatch.fnmatch(norm, inc) or fnmatch.fnmatch(norm, f"{inc.rstrip('/')}/*"):
                return True
        return False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class SubsystemConfigManager:
    """Manages parsing and validation of repository subsystem definitions."""

    def __init__(self, repo_path: Path):
        self.repo_path = repo_path.resolve()
        self.config_file = self.repo_path / ".brain" / "subsystems.yaml"

    def load_subsystems(self) -> Dict[str, SubsystemSpec]:
        if not self.config_file.exists():
            return self._get_default_subsystems()

        try:
            content = self.config_file.read_text(encoding="utf-8")
            data = yaml.safe_load(content) or {}
            subsystems_raw = data.get("subsystems", {})

            result: Dict[str, SubsystemSpec] = {}
            for name, spec_data in subsystems_raw.items():
                result[name] = SubsystemSpec(
                    name=name,
                    include=spec_data.get("include", []),
                    exclude=spec_data.get("exclude", []),
                    owners=spec_data.get("owners", []),
                    allowed_dependencies=spec_data.get("allowed_dependencies", []),
                    forbidden_dependencies=spec_data.get("forbidden_dependencies", []),
                    public_modules=spec_data.get("public_modules", []),
                )
            return result
        except Exception:
            return self._get_default_subsystems()

    def find_subsystem_for_file(self, rel_path: str, subsystems: Optional[Dict[str, SubsystemSpec]] = None) -> Optional[str]:
        specs = subsystems or self.load_subsystems()
        for name, spec in specs.items():
            if spec.matches_file(rel_path):
                return name
        return None

    def _get_default_subsystems(self) -> Dict[str, SubsystemSpec]:
        return {
            "api": SubsystemSpec(
                name="api",
                include=["apps/api/**"],
                owners=["@platform-api"],
                forbidden_dependencies=["workers"],
            ),
            "retrieval": SubsystemSpec(
                name="retrieval",
                include=["brain/retrieval/**", "brain/core/context_pack.py"],
                owners=["@search-platform"],
            ),
            "graph": SubsystemSpec(
                name="graph",
                include=["brain/graph/**", "brain/indexers/**"],
                owners=["@knowledge-platform"],
            ),
            "workers": SubsystemSpec(
                name="workers",
                include=["brain/workers/**"],
                owners=["@platform-runtime"],
                forbidden_dependencies=["api"],
            ),
        }
