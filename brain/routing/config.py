"""Configuration Loader for Selective Policy in Project Brain v0.5.3."""

from pathlib import Path
import yaml
from brain.routing.models import PolicyMode, SelectiveExecutionPolicy


def load_policy_config(config_path: Path | None = None) -> SelectiveExecutionPolicy:
    if config_path and config_path.exists():
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        mode_str = data.get("mode", "observe")
        return SelectiveExecutionPolicy(
            policy_version=data.get("version", "v0.5.3"),
            mode=PolicyMode(mode_str),
        )
    return SelectiveExecutionPolicy(mode=PolicyMode.OBSERVE)
