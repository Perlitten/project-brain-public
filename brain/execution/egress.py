"""Allowlist-enforcing HTTP(S) proxy for bounded execution egress (Team M2).

Egress is denied by default: the container backend runs every step with
``--network none``. A run whose capabilities carry an egress allowlist instead
runs on a per-run ``--internal`` Docker network whose only other member is this
proxy (a sidecar started from the Brain image, ``python -m brain.execution.egress``),
which is also attached to an upstream network. The step container gets
``HTTP(S)_PROXY`` pointing here and has no other route out.

The proxy:

- accepts ``CONNECT host:port`` (HTTPS and other TLS tunnels) and absolute-form
  plain HTTP requests (``GET http://host/path``), nothing else;
- allows a destination only when the host is listed exactly (no wildcards) and
  the port is listed with it, or is 80/443 when the entry names no port;
- resolves the host itself and connects only to the addresses it checked (in
  resolver order, falling back to the next one when a connection fails), refusing
  host names that resolve to any non-global address (loopback, private,
  link-local such as cloud metadata) unless that IP literal is listed itself;
- logs one JSON line per decision to stdout.

It uses the standard library only, so the sidecar needs nothing beyond the
Brain image.
"""
from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import re
import socket
import sys
from dataclasses import dataclass
from typing import Awaitable, Callable, Iterable, Mapping, Sequence

DEFAULT_PORTS = frozenset({80, 443})
PROXY_PORT = 3128
PROXY_ALIAS = "brain-egress"
READY_EVENT = "ready"
MAX_HEAD_BYTES = 16384
HEAD_TIMEOUT = 30.0
CONNECT_TIMEOUT = 15.0
IDLE_TIMEOUT = 300.0
_HOST = re.compile(r"^[a-z0-9]([a-z0-9-]{0,62})(\.[a-z0-9]([a-z0-9-]{0,62}))*$")
_HOP_HEADERS = {"connection", "keep-alive", "proxy-connection", "proxy-authorization", "proxy-authenticate", "te",
                "trailer", "upgrade"}

Resolver = Callable[[str, int], Awaitable[Sequence[str]]]


class EgressDenied(Exception):
    """A request the proxy refuses; ``status`` is the HTTP status sent back."""

    def __init__(self, reason: str, status: int = 403):
        super().__init__(reason)
        self.reason = reason
        self.status = status


def normalize_host(host: str) -> str:
    return host.strip().lower().rstrip(".")


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class EgressAllowlist:
    """Exact host (and port) allowlist. ``entries`` maps a host to its allowed ports."""

    entries: Mapping[str, frozenset[int]]

    @classmethod
    def parse(cls, values: Iterable[str]) -> EgressAllowlist:
        entries: dict[str, set[int]] = {}
        for raw in values:
            for item in str(raw).split(","):
                item = item.strip().lower()
                if not item:
                    continue
                host, _, port_text = item.partition(":")
                host = normalize_host(host)
                if not (_HOST.match(host) or _is_ip(host)) or len(host) > 253:
                    raise ValueError(f"Invalid egress host: {item}")
                if port_text:
                    if not port_text.isdigit() or not 0 < int(port_text) < 65536:
                        raise ValueError(f"Invalid egress port: {item}")
                    ports = {int(port_text)}
                else:
                    ports = set(DEFAULT_PORTS)
                entries.setdefault(host, set()).update(ports)
        return cls({host: frozenset(ports) for host, ports in entries.items()})

    def hosts(self) -> list[str]:
        return sorted(self.entries)

    def allows(self, host: str, port: int) -> bool:
        return port in self.entries.get(normalize_host(host), frozenset())


async def system_resolver(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    seen: list[str] = []
    for info in infos:
        address = str(info[4][0])
        if address not in seen:
            seen.append(address)
    return seen


async def check_destination(allowlist: EgressAllowlist, host: str, port: int, *, resolver: Resolver = system_resolver,
                            allow_private: bool = False) -> list[str]:
    """Return the checked addresses to connect to (in order), or raise ``EgressDenied``."""
    host = normalize_host(host)
    if not allowlist.allows(host, port):
        raise EgressDenied("Destination not in the egress allowlist")
    if _is_ip(host):
        return [host]  # an IP literal listed explicitly is the operator's choice
    try:
        addresses = list(await resolver(host, port))
    except (OSError, UnicodeError):
        raise EgressDenied("Destination host could not be resolved", status=502) from None
    if not addresses:
        raise EgressDenied("Destination host could not be resolved", status=502)
    if not allow_private:
        for address in addresses:
            try:
                ip = ipaddress.ip_address(address.split("%", 1)[0])
            except ValueError:
                raise EgressDenied("Destination resolved to an invalid address") from None
            if not ip.is_global:
                raise EgressDenied("Destination resolves to a non-public address")
    return addresses


def _log(event: str, **fields) -> None:
    sys.stdout.write(json.dumps({"event": event, **fields}, sort_keys=True) + "\n")
    sys.stdout.flush()


def _parse_target(method: str, target: str) -> tuple[str, int, str]:
    """Return (host, port, origin-form path) for a proxy request target."""
    if method == "CONNECT":
        host, sep, port_text = target.rpartition(":")
        if not sep or not port_text.isdigit() or not host:
            raise EgressDenied("CONNECT needs host:port", status=400)
        return host, int(port_text), ""
    lowered = target.lower()
    if not lowered.startswith("http://"):
        raise EgressDenied("Only CONNECT and absolute-form http:// requests are proxied", status=400)
    rest = target[len("http://"):]
    authority, slash, path = rest.partition("/")
    authority = authority.rsplit("@", 1)[-1]
    host, sep, port_text = authority.partition(":")
    if sep and not port_text.isdigit():
        raise EgressDenied("Invalid port", status=400)
    if not host:
        raise EgressDenied("Missing host", status=400)
    return host, int(port_text) if sep else 80, slash + path if slash else "/"


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await asyncio.wait_for(reader.read(65536), IDLE_TIMEOUT)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError):
        pass
    finally:
        try:
            if writer.can_write_eof():
                writer.write_eof()
        except OSError:
            pass


async def _close(writer: asyncio.StreamWriter) -> None:
    try:
        writer.close()
        await writer.wait_closed()
    except OSError:
        pass


class EgressProxy:
    def __init__(self, allowlist: EgressAllowlist, *, resolver: Resolver = system_resolver,
                 allow_private: bool = False, max_connections: int = 64):
        self.allowlist = allowlist
        self.resolver = resolver
        self.allow_private = allow_private
        self._slots = asyncio.Semaphore(max_connections)

    async def handle(self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
        async with self._slots:
            try:
                await self._handle(client_reader, client_writer)
            finally:
                await _close(client_writer)

    async def _handle(self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(client_reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT)
        except asyncio.LimitOverrunError:
            await self._reply(client_writer, 431, "Request head too large")
            return
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, OSError):
            return
        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        method = target = ""
        try:
            if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
                raise EgressDenied("Malformed request line", status=400)
            method, target = parts[0].upper(), parts[1]
            host, port, path = _parse_target(method, target)
            addresses = await check_destination(self.allowlist, host, port, resolver=self.resolver,
                                                allow_private=self.allow_private)
        except EgressDenied as denied:
            _log("egress", decision="deny", method=method, target=target[:300], reason=denied.reason)
            await self._reply(client_writer, denied.status, denied.reason)
            return
        upstream = address = None
        for address in addresses:
            try:
                upstream = await asyncio.wait_for(asyncio.open_connection(address, port), CONNECT_TIMEOUT)
                break
            except (OSError, asyncio.TimeoutError):
                continue
        if upstream is None:
            _log("egress", decision="error", method=method, host=normalize_host(host), port=port,
                 reason="Upstream connection failed")
            await self._reply(client_writer, 502, "Upstream connection failed")
            return
        upstream_reader, upstream_writer = upstream
        _log("egress", decision="allow", method=method, host=normalize_host(host), port=port, address=address)
        try:
            if method == "CONNECT":
                client_writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await client_writer.drain()
            else:
                headers = [line for line in lines[1:] if line and line.split(":", 1)[0].strip().lower()
                           not in _HOP_HEADERS]
                request = "\r\n".join([f"{method} {path} {parts[2]}", *headers, "Connection: close", "", ""])
                upstream_writer.write(request.encode("latin-1"))
                await upstream_writer.drain()
            await asyncio.gather(_pipe(client_reader, upstream_writer), _pipe(upstream_reader, client_writer))
        finally:
            await _close(upstream_writer)

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, status: int, reason: str) -> None:
        phrase = {400: "Bad Request", 403: "Forbidden", 431: "Request Header Fields Too Large",
                  502: "Bad Gateway"}.get(status, "Error")
        body = (reason + "\n").encode("utf-8")
        try:
            writer.write(f"HTTP/1.1 {status} {phrase}\r\nContent-Type: text/plain\r\nContent-Length: {len(body)}\r\n"
                         f"Connection: close\r\n\r\n".encode("latin-1") + body)
            await writer.drain()
        except OSError:
            pass

    async def start(self, host: str, port: int) -> asyncio.Server:
        return await asyncio.start_server(self.handle, host, port, limit=MAX_HEAD_BYTES)


async def serve(allowlist: EgressAllowlist, listen_host: str, listen_port: int, *, max_connections: int = 64) -> None:
    proxy = EgressProxy(allowlist, max_connections=max_connections)
    server = await proxy.start(listen_host, listen_port)
    _log(READY_EVENT, listen=f"{listen_host}:{listen_port}", allow=allowlist.hosts())
    async with server:
        await server.serve_forever()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m brain.execution.egress",
                                     description="Allowlist-enforcing HTTP(S) proxy for bounded execution.")
    parser.add_argument("--listen", default=f"0.0.0.0:{PROXY_PORT}", help="host:port to listen on")
    parser.add_argument("--allow", action="append", default=[], help="allowed host[:port]; repeatable or comma-separated")
    parser.add_argument("--max-connections", type=int, default=64)
    args = parser.parse_args(argv)
    try:
        allowlist = EgressAllowlist.parse(args.allow)
    except ValueError as exc:
        parser.error(str(exc))
    if not allowlist.entries:
        parser.error("at least one --allow entry is required")
    host, _, port_text = args.listen.rpartition(":")
    if not host or not port_text.isdigit():
        parser.error("--listen must be host:port")
    try:
        asyncio.run(serve(allowlist, host, int(port_text), max_connections=max(1, args.max_connections)))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
