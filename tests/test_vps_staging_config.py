"""Unit Tests for VPS Staging Config in Project Brain v0.5.3."""

from pathlib import Path
from deploy.vps_staging_smoke import verify_staging_config


def test_vps_staging_configuration():
    cfg_path = Path("deploy/staging-config.yaml")
    res = verify_staging_config(cfg_path)
    assert res["status"] == "passed"
    assert res["routing_mode"] == "observe"
    assert res["lab_execution_isolated"] is True
