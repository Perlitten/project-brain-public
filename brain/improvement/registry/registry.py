"""AgentBundle Registry and Immutable Alias Manager."""

import json
from pathlib import Path
from typing import Dict, Optional
from brain.improvement.models import AgentBundleManifest
from brain.improvement.registry.bundle import create_agent_bundle


class BundleRegistry:
    """Manages immutable AgentBundle manifests and alias pointers (champion, previous_champion)."""

    def __init__(self, registry_dir: Optional[Path] = None):
        self.registry_dir = registry_dir or Path("d:/Brain/project-brain/reports/improvement/registry")
        self.registry_dir.mkdir(parents=True, exist_ok=True)
        self.aliases_file = self.registry_dir / "aliases.json"
        self._bundles: Dict[str, AgentBundleManifest] = {}
        self._aliases: Dict[str, str] = self._load_aliases()

        # Initialize default champion bundle
        if "champion" not in self._aliases:
            champ = create_agent_bundle("bundle_champion_v070")
            self.register_bundle(champ)
            self._aliases["champion"] = champ.bundle_id
            self._save_aliases()

    def _load_aliases(self) -> Dict[str, str]:
        if not self.aliases_file.exists():
            return {}
        try:
            return json.loads(self.aliases_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_aliases(self):
        self.aliases_file.write_text(json.dumps(self._aliases, indent=2), encoding="utf-8")

    def register_bundle(self, bundle: AgentBundleManifest) -> Path:
        """Registers a new immutable bundle manifest."""
        self._bundles[bundle.bundle_id] = bundle
        target_file = self.registry_dir / f"{bundle.bundle_id}.json"
        target_file.write_text(json.dumps(bundle.model_dump(), indent=2), encoding="utf-8")
        return target_file

    def get_bundle(self, bundle_id: str) -> Optional[AgentBundleManifest]:
        target_file = self.registry_dir / f"{bundle_id}.json"
        if not target_file.exists():
            return self._bundles.get(bundle_id)
        try:
            raw = json.loads(target_file.read_text(encoding="utf-8"))
            return AgentBundleManifest(**raw)
        except Exception:
            return None

    def get_alias(self, alias_name: str) -> Optional[str]:
        return self._aliases.get(alias_name)

    def set_alias(self, alias_name: str, bundle_id: str) -> bool:
        """Updates alias pointer (e.g. champion -> bundle_id) after human approval."""
        if alias_name == "champion":
            curr_champ = self._aliases.get("champion")
            if curr_champ:
                self._aliases["previous_champion"] = curr_champ
        self._aliases[alias_name] = bundle_id
        self._save_aliases()
        return True
