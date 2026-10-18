"""Static guards for the team deployment's HTTPS front (deploy/team/).

The behaviour itself is exercised end to end by scripts/team_proxy_check.py
(Caddy, the API and the dashboard in containers; the "Team proxy" workflow).
These tests need no Docker: they pin the wiring that check relies on.
"""
from __future__ import annotations

import ipaddress
import os
import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import team_proxy_check  # noqa: E402

TEAM = ROOT / "deploy" / "team"
CADDYFILE = (TEAM / "Caddyfile").read_text(encoding="utf-8")


class _ComposeLoader(yaml.SafeLoader):
    """Reads compose files that use the !reset / !override merge tags."""


def _tagged(loader: yaml.SafeLoader, node: yaml.Node):
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_scalar(node)


for _tag in ("!reset", "!override"):
    _ComposeLoader.add_constructor(_tag, _tagged)


def _compose(path: Path) -> dict:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=_ComposeLoader)  # noqa: S506 - SafeLoader subclass


def _default(value: str) -> str:
    """``${NAME:-default}`` -> ``default``."""
    match = re.fullmatch(r"\$\{[A-Z_]+:-([^}]*)\}", value.strip())
    assert match, value
    return match.group(1)


def test_derived_caddyfile_only_swaps_acme_for_local_certificates():
    derived = team_proxy_check.derive_caddyfile(CADDYFILE)
    lines = derived.splitlines()
    assert lines[0] == "{"
    assert lines[1:3] == ["\tlocal_certs", "\tskip_install_trust"]
    assert "\n".join(lines[:1] + lines[3:]) + "\n" == CADDYFILE.replace("\r\n", "\n")


def test_derived_caddyfile_needs_a_global_block():
    with pytest.raises(ValueError):
        team_proxy_check.derive_caddyfile("example.com {\n\trespond ok\n}\n")
    with pytest.raises(ValueError):
        team_proxy_check.derive_caddyfile("# only a comment\n")
    assert team_proxy_check.derive_caddyfile("# note\n{\n}\n").splitlines()[:3] == ["# note", "{", "\tlocal_certs"]


def test_caddy_body_limit_matches_the_api():
    from brain.config.settings import Settings

    match = re.search(r"max_size\s+(\d+)MiB", CADDYFILE)
    assert match, "the Caddyfile must cap request bodies"
    assert int(match.group(1)) * 1024 * 1024 == Settings.model_fields["API_MAX_REQUEST_BODY_BYTES"].default
    assert int(match.group(1)) * 1024 * 1024 == team_proxy_check.BODY_LIMIT
    # Both sites import the shared snippet that carries the limit and headers.
    assert CADDYFILE.count("import edge") == 2


def test_caddy_gates_health_behind_the_admin_diagnostics_route():
    assert re.search(r"@probe path /health /ready\b", CADDYFILE)
    assert "uri /api/status/diagnostics" in CADDYFILE
    assert re.search(r"@denied status 401 403\s+handle_response @denied \{\s+respond 404", CADDYFILE)
    router = (ROOT / "apps" / "api" / "routers" / "core_status.py").read_text(encoding="utf-8")
    assert '@router.get("/api/status/diagnostics", dependencies=ADMIN_READ)' in router
    assert "team_administrator_access()" in router.split("ADMIN_READ =", 1)[1].split("\n", 1)[0]


def test_caddy_keeps_streaming_and_sets_security_headers():
    assert "flush_interval -1" in CADDYFILE
    # "?": defaults only, so the API's own headers and the dashboard's stricter ones pass unchanged.
    for header in ('?Strict-Transport-Security "max-age=', '?X-Content-Type-Options "nosniff"',
                   '?X-Frame-Options "DENY"', '?Referrer-Policy "', "?Permissions-Policy "):
        assert header in CADDYFILE, header


def test_api_trusts_exactly_caddy_and_web_on_the_edge_network():
    overlay = _compose(TEAM / "docker-compose.team.yml")
    services = overlay["services"]
    subnet = ipaddress.ip_network(_default(overlay["networks"]["edge"]["ipam"]["config"][0]["subnet"]))
    caddy_ip = _default(services["caddy"]["networks"]["edge"]["ipv4_address"])
    web_ip = _default(services["web"]["networks"]["edge"]["ipv4_address"])
    assert ipaddress.ip_address(caddy_ip) in subnet and ipaddress.ip_address(web_ip) in subnet
    trusted = services["api"]["environment"]["BRAIN_TRUSTED_PROXY_IPS"]
    assert [_default(part) for part in trusted.split(",")] == [caddy_ip, web_ip]
    assert trusted == "${BRAIN_CADDY_IP:-%s},${BRAIN_WEB_IP:-%s}" % (caddy_ip, web_ip)
    assert services["caddy"]["networks"]["edge"]["ipv4_address"].startswith("${BRAIN_CADDY_IP:-")
    assert services["web"]["networks"]["edge"]["ipv4_address"].startswith("${BRAIN_WEB_IP:-")
    # The API keeps the datastores' network; caddy and web never join it.
    assert services["api"]["networks"] == ["default", "edge"]
    assert set(services["caddy"]["networks"]) == {"edge"}
    assert set(services["web"]["networks"]) == {"edge"}


def test_env_template_documents_the_edge_addresses():
    template = (TEAM / ".env.team.example").read_text(encoding="utf-8")
    names = {line.split("=", 1)[0] for line in template.splitlines() if "=" in line and not line.startswith("#")}
    assert {"BRAIN_EDGE_SUBNET", "BRAIN_CADDY_IP", "BRAIN_WEB_IP"} <= names


def test_check_stack_never_reuses_production_names():
    override = _compose(ROOT / "deploy" / "team-proxy-check" / "docker-compose.yml")["services"]
    stacks = [_compose(ROOT / "deploy" / "docker-compose.prod.yml"), _compose(TEAM / "docker-compose.team.yml")]
    for stack in stacks:
        for name, service in stack["services"].items():
            if "container_name" not in service or service.get("profiles"):
                continue
            assert override[name]["container_name"].startswith("brain-proxycheck-"), name
            # No bind mounts or published ports reach the check stack.
            for key in ("volumes", "ports"):
                if key in service and name in ("api", "worker", "caddy"):
                    assert override[name][key] == [], (name, key)
    for name in ("client-a", "client-b", "intruder", "idp"):
        assert override[name]["container_name"] == f"brain-proxycheck-{name}"


def test_check_addresses_never_collide_with_the_overlay_defaults():
    overlay = _compose(TEAM / "docker-compose.team.yml")
    default_subnet = ipaddress.ip_network(_default(overlay["networks"]["edge"]["ipam"]["config"][0]["subnet"]))
    for name in ("BRAIN_EDGE_SUBNET", "PROXYCHECK_OUTSIDE_SUBNET"):
        assert not ipaddress.ip_network(team_proxy_check.NETWORK[name]).overlaps(default_subnet)
    edge = ipaddress.ip_network(team_proxy_check.NETWORK["BRAIN_EDGE_SUBNET"])
    for name in ("BRAIN_CADDY_IP", "BRAIN_WEB_IP", "PROXYCHECK_INTRUDER_IP"):
        assert ipaddress.ip_address(team_proxy_check.NETWORK[name]) in edge
    assert os.path.basename(team_proxy_check.COMPOSE_FILES[1]) == "docker-compose.team.yml"
