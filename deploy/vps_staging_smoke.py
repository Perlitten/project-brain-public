"""VPS Staging Environment Smoke Test Script for Project Brain v0.5.3."""

import json
from pathlib import Path
import yaml


def verify_staging_config(config_path: Path) -> dict:
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert data["routing"]["mode"] == "observe"
    assert data["execution"]["authoritative_mode"] == "legacy"
    assert data["execution"]["shadow_enabled"] is False
    assert "ARBITRARY_EXEC" in data["execution"]["denied_capabilities"]
    return {
        "status": "passed",
        "target_host": data["target_host"],
        "routing_mode": data["routing"]["mode"],
        "lab_execution_isolated": True,
    }


def main():
    cfg = Path(__file__).parent / "staging-config.yaml"
    res = verify_staging_config(cfg)
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
