#!/usr/bin/env python3
"""Check the team deployment's HTTPS front (deploy/team/Caddyfile) in throwaway containers.

    python3 scripts/team_proxy_check.py [--keep] [--build-network host]

Brings up the real production stack plus the real team overlay
(deploy/docker-compose.prod.yml + deploy/team/docker-compose.team.yml) with the
test-only override deploy/team-proxy-check/docker-compose.yml under compose
project ``brain-proxycheck``: PostgreSQL, Redis, the API, the dashboard and
Caddy running the real Caddyfile with local certificates instead of ACME. Then
it plays outside visitors against Caddy and asserts what the front promises:

  - TLS for both names, HTTP redirected to HTTPS, unknown names refused;
  - /health and /ready reachable through the proxy only with an admin key;
  - the API keys its throttles by the visitor's address when caddy (or the
    dashboard) forwards it, and ignores X-Forwarded-For from anyone else;
  - single sign-on redirect URLs and cookies say https;
  - oversized bodies (declared or chunked) are refused;
  - security headers are present, and the app's stricter ones are kept.

Exits 0 when every check passes, 1 when one fails (with container logs), 2 when
the stack cannot start. Needs docker with compose v2.24+ and nothing else
(stdlib only). Every container, network and volume is removed afterwards unless
--keep. Runs from Linux, macOS, or Windows (py -3) against any docker engine:
there are no bind mounts. See docs/operations/TEAM_DEPLOY.md.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "brain-proxycheck"
CADDYFILE = ROOT / "deploy" / "team" / "Caddyfile"
COMPOSE_FILES = (
    ROOT / "deploy" / "docker-compose.prod.yml",
    ROOT / "deploy" / "team" / "docker-compose.team.yml",
    ROOT / "deploy" / "team-proxy-check" / "docker-compose.yml",
)
SERVICES = ("postgres", "redis", "api", "idp", "web", "client-a", "client-b", "intruder")
CADDY = f"{PROJECT}-caddy"
CLIENT_A = f"{PROJECT}-client-a"
CLIENT_B = f"{PROJECT}-client-b"
INTRUDER = f"{PROJECT}-intruder"
REDIS = f"{PROJECT}-redis"
WEB_DOMAIN = "brain.test"
API_DOMAIN = "api.test"
CA_IN_CLIENT = "/tmp/proxycheck-ca.crt"
CA_IN_CADDY = "/data/caddy/pki/authorities/local/root.crt"
# Throttle budgets (apps/api/routers/team_invitations.py ACCEPT_LIMIT, team_oidc.py OIDC_LIMIT).
ACCEPT_LIMIT = 10
BODY_LIMIT = 25 * 1024 * 1024
# Addresses that differ from the overlay's defaults, so a real deployment on the same host never collides.
NETWORK = {
    "BRAIN_EDGE_SUBNET": "172.31.249.0/24",
    "BRAIN_CADDY_IP": "172.31.249.10",
    "BRAIN_WEB_IP": "172.31.249.11",
    "PROXYCHECK_INTRUDER_IP": "172.31.249.30",
    "PROXYCHECK_OUTSIDE_SUBNET": "172.31.248.0/24",
    "PROXYCHECK_CADDY_OUTSIDE_IP": "172.31.248.10",
    "PROXYCHECK_CLIENT_A_IP": "172.31.248.21",
    "PROXYCHECK_CLIENT_B_IP": "172.31.248.22",
}
TEST_GLOBALS = ("local_certs", "skip_install_trust")


def derive_caddyfile(text: str) -> str:
    """The real Caddyfile with local certificates instead of ACME; nothing else changes."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped != "{":
            raise ValueError("the Caddyfile must open with a global options block")
        derived = lines[: index + 1] + [f"\t{option}" for option in TEST_GLOBALS] + lines[index + 1:]
        return "\n".join(derived) + "\n"
    raise ValueError("the Caddyfile is empty")


# Runs inside a client container (python from the API image): one HTTP(S)
# request, answered as one JSON line. The script feeds it on stdin.
PROBE = r'''
import http.client, json, socket, ssl

def body_of(size, chunk=1 << 20):
    sent = 0
    while sent < size:
        part = min(chunk, size - sent)
        sent += part
        yield b"x" * part

def run(spec):
    host = spec["host"]
    https = spec.get("scheme", "https") == "https"
    port = spec.get("port") or (443 if https else 80)
    headers = dict(spec.get("headers") or {})
    headers.setdefault("Host", host)
    body = None
    if "body" in spec:
        body = spec["body"].encode()
    elif spec.get("size"):
        body = body_of(spec["size"])
        if not spec.get("chunked"):
            headers["Content-Length"] = str(spec["size"])
    try:
        sock = socket.create_connection((spec.get("connect", host), port), timeout=60)
        if https:
            context = ssl.create_default_context(cafile=spec["ca"])
            sock = context.wrap_socket(sock, server_hostname=spec.get("sni", host))
    except (OSError, ssl.SSLError) as exc:
        return {"error": type(exc).__name__ + ": " + str(exc)[:300]}
    conn = http.client.HTTPConnection(host, port, timeout=60)
    conn.sock = sock
    send_error = None
    try:
        conn.request(spec.get("method", "GET"), spec.get("path", "/"), body=body, headers=headers,
                     encode_chunked=bool(spec.get("chunked")))
    except OSError as exc:
        send_error = type(exc).__name__
    try:
        resp = conn.getresponse()
        data = resp.read(4096)
    except (OSError, http.client.HTTPException) as exc:
        return {"error": (send_error or "") + " " + type(exc).__name__ + ": " + str(exc)[:300]}
    return {
        "status": resp.status,
        "headers": {k.lower(): v for k, v in resp.getheaders()},
        "cookies": resp.headers.get_all("Set-Cookie") or [],
        "body": data.decode("utf-8", "replace"),
        "send_error": send_error,
    }

print(json.dumps(run(SPEC)))
'''


class StackError(RuntimeError):
    pass


class CheckFailed(AssertionError):
    pass


class Stack:
    def __init__(self, env: dict[str, str], env_file: Path) -> None:
        self.env = env
        self.base = ["docker", "compose", "-p", PROJECT, "--project-directory", str(ROOT),
                     "--env-file", str(env_file)]
        for path in COMPOSE_FILES:
            self.base += ["-f", str(path)]

    def compose(self, *args: str, check: bool = True, quiet: bool = False) -> subprocess.CompletedProcess[str]:
        out = subprocess.PIPE if quiet else None
        result = subprocess.run([*self.base, *args], env=self.env, text=True, stdout=out, stderr=out)
        if check and result.returncode != 0:
            raise StackError(f"docker compose {' '.join(args)} exited {result.returncode}")
        return result

    def docker(self, *args: str, stdin: str | None = None, check: bool = True) -> str:
        result = subprocess.run(["docker", *args], env=self.env, input=stdin, text=True,
                                capture_output=True, encoding="utf-8", errors="replace")
        if check and result.returncode != 0:
            raise StackError(f"docker {' '.join(args[:2])} exited {result.returncode}: {result.stderr.strip()[:500]}")
        return result.stdout

    def request(self, client: str, **spec: Any) -> dict[str, Any]:
        spec.setdefault("ca", CA_IN_CLIENT)
        out = self.docker("exec", "-i", client, "python", "-", stdin=f"SPEC = {spec!r}\n{PROBE}")
        return json.loads(out.strip().splitlines()[-1])

    def flush_throttles(self) -> None:
        self.docker("exec", REDIS, "redis-cli", "FLUSHALL")


def environment(args: argparse.Namespace) -> dict[str, str]:
    env = dict(os.environ)
    env.update(NETWORK)
    env.update({
        "COMPOSE_PROJECT_NAME": PROJECT,
        "BRAIN_IMAGE_TAG": "proxycheck",
        "BRAIN_BUILD_SHA": "proxycheck",
        "BRAIN_SOURCE_DIGEST": "proxycheck",
        "BRAIN_DEPLOY_DIGEST": "proxycheck",
        "BRAIN_WEB_DOMAIN": WEB_DOMAIN,
        "BRAIN_API_DOMAIN": API_DOMAIN,
        "BRAIN_ACME_EMAIL": "proxy-check@example.com",
        "POSTGRES_PASSWORD": secrets.token_hex(16),
        "NEO4J_PASSWORD": secrets.token_hex(16),
        "PROJECT_BRAIN_API_KEY": secrets.token_hex(24),
        "PROJECT_BRAIN_WEBHOOK_TOKEN": secrets.token_hex(32),
        "PROXYCHECK_OIDC_SECRET": secrets.token_hex(16),
        "WEB_BASIC_AUTH": f"proxycheck:{secrets.token_hex(12)}",
        "BRAIN_TIMEZONE": "UTC",
        "PROXYCHECK_BUILD_NETWORK": args.build_network,
    })
    return env


def start(stack: Stack, args: argparse.Namespace) -> None:
    stack.compose("down", "-v", "--remove-orphans", check=False, quiet=True)
    if not args.no_build:
        stack.compose("build", "api", "web")
    stack.compose("up", "-d", "--wait", "--wait-timeout", "300", *SERVICES)
    # Caddy gets the derived Caddyfile before its first start.
    stack.compose("create", "--no-recreate", "caddy")
    with tempfile.TemporaryDirectory() as tmp:
        derived = Path(tmp) / "Caddyfile"
        derived.write_text(derive_caddyfile(CADDYFILE.read_text(encoding="utf-8")), encoding="utf-8", newline="\n")
        stack.docker("cp", str(derived), f"{CADDY}:/etc/caddy/Caddyfile")
    stack.compose("start", "caddy")
    ca = ""
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        ca = stack.docker("exec", CADDY, "cat", CA_IN_CADDY, check=False)
        if "BEGIN CERTIFICATE" in ca:
            break
        time.sleep(1)
    else:
        raise StackError("caddy issued no local certificate authority within 60s")
    for client in (CLIENT_A, CLIENT_B):
        stack.docker("exec", "-i", client, "sh", "-c", f"cat > {CA_IN_CLIENT}", stdin=ca)
    # Certificates for both names are issued right after start.
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if all("status" in stack.request(CLIENT_A, host=host, path="/robots.txt") for host in (WEB_DOMAIN, API_DOMAIN)):
            return
        time.sleep(1)
    raise StackError("caddy did not serve TLS for both names within 60s")


# ── checks ──────────────────────────────────────────────────────────────────

def expect(condition: bool, message: str, response: Any = None) -> None:
    if not condition:
        detail = f"\n    response: {json.dumps(response)[:1500]}" if response is not None else ""
        raise CheckFailed(message + detail)


def check_tls_and_hosts(stack: Stack) -> None:
    for host in (WEB_DOMAIN, API_DOMAIN):
        plain = stack.request(CLIENT_A, scheme="http", host=host, path="/health")
        expect(plain.get("status") in (301, 308) and plain["headers"].get("location") == f"https://{host}/health",
               f"http://{host} redirects to https", plain)
    unknown_sni = stack.request(CLIENT_A, host=API_DOMAIN, sni="unknown.test", path="/health")
    expect("error" in unknown_sni, "a TLS handshake for an unknown name fails", unknown_sni)
    # A known certificate name with another Host header: Caddy matches no site and answers an empty 200 itself.
    wrong_host = stack.request(CLIENT_A, host=API_DOMAIN, path="/health", headers={"Host": "unknown.test"})
    expect(wrong_host.get("status") == 200 and wrong_host.get("body") == "" and "content-type" not in wrong_host["headers"],
           "an unknown Host header over https reaches no upstream", wrong_host)
    # Over plain HTTP every name is only sent to https, where the handshake above fails.
    wrong_host = stack.request(CLIENT_A, scheme="http", host=API_DOMAIN, path="/health", headers={"Host": "unknown.test"})
    expect(wrong_host.get("status") in (301, 308) and wrong_host.get("body") == ""
           and wrong_host["headers"].get("location") == "https://unknown.test/health",
           "an unknown Host header over http is only redirected", wrong_host)


def check_health(stack: Stack, api_key: str) -> None:
    for path in ("/health", "/ready"):
        anonymous = stack.request(CLIENT_A, host=API_DOMAIN, path=path)
        expect(anonymous.get("status") == 404 and not anonymous.get("body"),
               f"{path} without a key answers an empty 404 through the proxy", anonymous)
        wrong = stack.request(CLIENT_A, host=API_DOMAIN, path=path, headers={"X-API-Key": "not-a-key"})
        expect(wrong.get("status") == 404, f"{path} with a wrong key answers 404", wrong)
    health = stack.request(CLIENT_A, host=API_DOMAIN, path="/health", headers={"X-API-Key": api_key})
    expect(health.get("status") == 200 and '"mode"' in health.get("body", ""),
           "/health with the server key reaches the API", health)
    # The API sets its own security headers; caddy only fills gaps, so they arrive unchanged.
    expect(health["headers"].get("strict-transport-security") == "max-age=63072000; includeSubDomains",
           "the API's own Strict-Transport-Security passes caddy unchanged", health)
    expect(health["headers"].get("x-content-type-options") == "nosniff", "API answers carry nosniff", health)
    expect(health["headers"].get("x-frame-options") == "DENY", "API answers carry X-Frame-Options", health)


def burst(send: Callable[[int], dict[str, Any]], expected: int, label: str) -> None:
    for attempt in range(ACCEPT_LIMIT):
        got = send(attempt)
        expect(got.get("status") == expected, f"{label}: attempt {attempt + 1} answers {expected}", got)
    got = send(ACCEPT_LIMIT)
    expect(got.get("status") == 429, f"{label}: attempt {ACCEPT_LIMIT + 1} is throttled (429)", got)


def spoofed(attempt: int) -> dict[str, str]:
    return {"X-Forwarded-For": f"203.0.113.{attempt + 1}", "X-Real-IP": f"198.51.100.{attempt + 1}"}


def check_client_address_via_api(stack: Stack) -> None:
    stack.flush_throttles()

    def accept(client: str, attempt: int) -> dict[str, Any]:
        headers = {"Content-Type": "application/json", **spoofed(attempt)}
        return stack.request(client, host=API_DOMAIN, method="POST", path="/team/invitations/accept",
                             headers=headers, body=json.dumps({"token": f"pbi_proxycheck{attempt:04d}"}))

    # A changing forged X-Forwarded-For does not buy client A fresh budgets...
    burst(lambda n: accept(CLIENT_A, n), 403, "API through caddy, client A with forged X-Forwarded-For")
    # ...and client A's budget is not client B's (it would be if the API keyed on caddy's address).
    got = accept(CLIENT_B, 0)
    expect(got.get("status") == 403, "API through caddy: client B keeps its own budget", got)


def check_client_address_via_web(stack: Stack) -> None:
    stack.flush_throttles()

    def accept(client: str, attempt: int) -> dict[str, Any]:
        headers = {"Content-Type": "application/x-www-form-urlencoded", "Origin": f"https://{WEB_DOMAIN}",
                   **spoofed(attempt)}
        return stack.request(client, host=WEB_DOMAIN, method="POST", path="/invite", headers=headers,
                             body=f"token=pbi_proxycheck{attempt:04d}")

    burst(lambda n: accept(CLIENT_A, n), 403, "dashboard /invite through caddy, client A with forged X-Forwarded-For")
    got = accept(CLIENT_B, 0)
    expect(got.get("status") == 403, "dashboard /invite through caddy: client B keeps its own budget", got)
    expect(got["headers"].get("referrer-policy") == "no-referrer", "the app's stricter Referrer-Policy is kept", got)


def check_untrusted_peer(stack: Stack) -> None:
    stack.flush_throttles()

    def accept(attempt: int) -> dict[str, Any]:
        headers = {"Content-Type": "application/json", **spoofed(attempt)}
        return stack.request(INTRUDER, scheme="http", host="api", port=8000, method="POST",
                             path="/team/invitations/accept", headers=headers,
                             body=json.dumps({"token": f"pbi_proxycheck{attempt:04d}"}))

    burst(accept, 403, "API called directly by an untrusted container with forged X-Forwarded-For")


def check_oidc_and_cookies(stack: Stack, basic_auth: str) -> None:
    stack.flush_throttles()
    start = stack.request(CLIENT_A, host=WEB_DOMAIN, path="/auth/oidc/start")
    location = start.get("headers", {}).get("location", "")
    expect(start.get("status") == 303 and location.startswith("http://127.0.0.1:9999/authorize?"),
           "single sign-on starts with a redirect to the provider", start)
    expected = "redirect_uri=https%3A%2F%2F" + WEB_DOMAIN + "%2Fauth%2Foidc%2Fcallback"
    expect(expected in location, "the provider is told an https callback on the dashboard's name", start)
    flow = [c for c in start.get("cookies", []) if c.startswith("pb_oidc=")]
    expect(len(flow) == 1 and "secure" in flow[0].lower() and "httponly" in flow[0].lower(),
           "the single sign-on cookie is Secure and HttpOnly", start)

    user, password = basic_auth.split(":", 1)
    login = stack.request(CLIENT_A, host=WEB_DOMAIN, method="POST", path="/__login",
                          headers={"Content-Type": "application/x-www-form-urlencoded"},
                          body=f"username={user}&password={password}&next=%2F")
    session = [c for c in login.get("cookies", []) if c.startswith("pb_session=")]
    expect(login.get("status") == 303 and len(session) == 1 and "secure" in session[0].lower(),
           "the dashboard session cookie is Secure", login)
    location = login["headers"].get("location", "")
    expect(location.startswith("/") or location.startswith(f"https://{WEB_DOMAIN}/"),
           "the sign-in redirect stays on https", login)
    # The dashboard sets few security headers of its own; caddy's defaults cover it.
    for name, value in (("strict-transport-security", "max-age=63072000"), ("x-content-type-options", "nosniff"),
                        ("x-frame-options", "DENY"), ("referrer-policy", "strict-origin-when-cross-origin")):
        expect(login["headers"].get(name) == value, f"dashboard answers carry {name}: {value}", login)


def check_body_limit(stack: Stack) -> None:
    target = {"host": API_DOMAIN, "method": "POST", "path": "/team/invitations/accept",
              "headers": {"Content-Type": "application/json"}}
    small = stack.request(CLIENT_A, chunked=True, size=1024 * 1024, **target)
    expect(small.get("status") not in (None, 413), "a 1 MiB chunked body passes the proxy", small)
    for chunked in (False, True):
        big = stack.request(CLIENT_A, chunked=chunked, size=BODY_LIMIT + 1024 * 1024, **target)
        label = "chunked" if chunked else "declared"
        expect(big.get("status") == 413, f"a {label} body over 25 MiB is refused with 413", big)


CHECKS: tuple[tuple[str, Callable[..., None]], ...] = (
    ("TLS, redirects and unknown names", check_tls_and_hosts),
    ("health checks gated", check_health),
    ("client address through caddy to the API", check_client_address_via_api),
    ("client address through caddy and the dashboard", check_client_address_via_web),
    ("forged X-Forwarded-For from an untrusted peer", check_untrusted_peer),
    ("single sign-on redirect and secure cookies", check_oidc_and_cookies),
    ("request body limit", check_body_limit),
)


def run_checks(stack: Stack) -> list[str]:
    failures = []
    for name, check in CHECKS:
        kwargs: dict[str, str] = {}
        if check is check_health:
            kwargs["api_key"] = stack.env["PROJECT_BRAIN_API_KEY"]
        if check is check_oidc_and_cookies:
            kwargs["basic_auth"] = stack.env["WEB_BASIC_AUTH"]
        began = time.monotonic()
        try:
            check(stack, **kwargs)
        except CheckFailed as exc:
            failures.append(f"{name}: {exc}")
            print(f"FAIL {name} ({time.monotonic() - began:.1f}s)\n  {exc}", flush=True)
        else:
            print(f"ok   {name} ({time.monotonic() - began:.1f}s)", flush=True)
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--keep", action="store_true", help="leave the stack running for inspection")
    parser.add_argument("--no-build", action="store_true", help="reuse brain-api:proxycheck and brain-web:proxycheck")
    parser.add_argument("--build-network", default="default",
                        help="network for image builds (host for daemons without bridge NAT)")
    args = parser.parse_args(argv)
    began = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp:
        # An empty env file, so a .env in the checkout never leaks into the test stack.
        env_file = Path(tmp) / "proxycheck.env"
        env_file.write_text("", encoding="utf-8")
        stack = Stack(environment(args), env_file)
        status = 0
        try:
            start(stack, args)
            failures = run_checks(stack)
            status = 1 if failures else 0
        except StackError as exc:
            print(f"stack error: {exc}", file=sys.stderr)
            status = 2
        if status:
            stack.compose("ps", "-a", check=False)
            stack.compose("logs", "--no-color", "--tail", "80", "api", "web", "caddy", check=False)
        if not args.keep:
            stack.compose("down", "-v", "--remove-orphans", check=False, quiet=True)
    verdict = "passed" if status == 0 else "FAILED"
    print(f"team proxy check {verdict} in {time.monotonic() - began:.0f}s", flush=True)
    return status


if __name__ == "__main__":
    sys.exit(main())
