"""Retrieval v6 P3 — deterministic file-card construction.

A *file card* is a compact structured document describing a file's ROLE in the
system (surface, role, routes, CLI flags, exported symbols, imports, related tests,
domain terms) — as opposed to chunk vectors which describe "which fragment looks
similar". Cards are built deterministically from already-extracted features (Symbol
rows, path, surface); no LLM. Two formats (``code_shaped`` default, ``prose``) are
assembled from the *same* fields so a format A/B isolates representation shape.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Dict, List

from brain.search.filters import is_test_path, paired_test_path
from brain.search.surfaces import classify_surface

# Bump when card field assembly changes so card_hash invalidates stale cards.
CARD_EXTRACTOR_VERSION = "p3.1"

_DOMAIN_STOP = {
    "the", "and", "for", "with", "from", "test", "tests", "src", "py", "main",
    "init", "util", "utils", "helper", "helpers", "common", "core", "base",
    "get", "set", "run", "api", "app",
}


def _tokens(text: str) -> List[str]:
    """Split snake/camel/path identifiers into lowercase domain tokens."""
    parts = re.split(r"[^A-Za-z0-9]+", text)
    out: List[str] = []
    for p in parts:
        # split camelCase
        for w in re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+", p) or [p]:
            wl = w.lower()
            if len(wl) >= 3 and wl not in _DOMAIN_STOP:
                out.append(wl)
    # de-dup preserve order
    seen = set()
    res = []
    for t in out:
        if t not in seen:
            seen.add(t)
            res.append(t)
    return res


def derive_file_role(path: str, surface: str, symbols_by_kind: Dict[str, List[str]]) -> str:
    """Deterministic file role from path-derived surface + symbol evidence."""
    norm = path.replace("\\", "/").lower()
    if surface == "tests" or is_test_path(path):
        return "test"
    if symbols_by_kind.get("script_entrypoint") or symbols_by_kind.get("cli_flag") or symbols_by_kind.get("cli_command"):
        return "cli_entrypoint"
    if surface == "scripts":
        return "script"
    if symbols_by_kind.get("api_route"):
        return "api_handler"
    if surface == "server":
        return "api_handler"
    if surface == "engine":
        return "engine"
    if surface == "extension":
        return "extension"
    if surface == "config":
        return "config"
    if surface == "docs":
        return "docs"
    if surface == "database":
        return "engine"
    if norm.endswith((".csv", ".json", ".yaml", ".yml", ".parquet")):
        return "data"
    return "unknown"


def _module_of(path: str) -> str:
    norm = path.replace("\\", "/")
    parent = norm.rsplit("/", 1)[0] if "/" in norm else ""
    return parent


@dataclass
class CardSpec:
    path: str
    surface: str
    file_role: str
    card_text: str
    card_text_format: str
    card_hash: str
    domain_terms: List[str] = field(default_factory=list)


def build_card_fields(path: str, symbols_by_kind: Dict[str, List[str]]) -> dict:
    """Assemble the deterministic structured fields shared by both card formats."""
    surface = classify_surface(path)
    role = derive_file_role(path, surface, symbols_by_kind)
    module = _module_of(path)
    classes = symbols_by_kind.get("class", [])
    functions = symbols_by_kind.get("function", [])
    routes = symbols_by_kind.get("api_route", [])
    cli_flags = symbols_by_kind.get("cli_flag", [])
    cli_commands = symbols_by_kind.get("cli_command", [])
    entrypoints = symbols_by_kind.get("script_entrypoint", [])
    imports = symbols_by_kind.get("import_target", [])
    paired = paired_test_path(path) if not is_test_path(path) else None

    domain_src = " ".join([path, module] + classes + functions + routes + cli_flags + cli_commands)
    domain_terms = _tokens(domain_src)[:15]

    return {
        "path": path,
        "surface": surface,
        "role": role,
        "module": module,
        "classes": classes,
        "functions": functions,
        "routes": routes,
        "cli_flags": cli_flags,
        "cli_commands": cli_commands,
        "entrypoints": entrypoints,
        "imports": imports,
        "paired_test": paired,
        "domain_terms": domain_terms,
    }


def _render_code_shaped(f: dict) -> str:
    lines: List[str] = []
    lines.append(f"# file: {f['path']}")
    lines.append(f"# surface: {f['surface']} | role: {f['role']} | module: {f['module']}")
    for r in f["routes"][:20]:
        lines.append(f"@route {r}")
    for c in f["classes"][:20]:
        lines.append(f"class {c}")
    for fn in f["functions"][:30]:
        lines.append(f"def {fn}")
    for cmd in f["cli_commands"][:15]:
        lines.append(f"@command {cmd}")
    for flag in f["cli_flags"][:20]:
        lines.append(f"--{flag}")
    for ep in f["entrypoints"][:3]:
        lines.append(f"if __name__ == '__main__': {ep}")
    for imp in f["imports"][:20]:
        lines.append(f"import {imp}")
    if f["paired_test"]:
        lines.append(f"# tested_by: {f['paired_test']}")
    if f["domain_terms"]:
        lines.append(f"# domain: {', '.join(f['domain_terms'])}")
    return "\n".join(lines)


def _render_prose(f: dict) -> str:
    parts: List[str] = []
    parts.append(f"File {f['path']} is a {f['surface']} {f['role']} in module {f['module'] or 'root'}.")
    if f["routes"]:
        parts.append("It defines API routes " + ", ".join(f["routes"][:20]) + ".")
    if f["classes"]:
        parts.append("It defines classes " + ", ".join(f["classes"][:20]) + ".")
    if f["functions"]:
        parts.append("It defines functions " + ", ".join(f["functions"][:30]) + ".")
    if f["cli_commands"] or f["cli_flags"]:
        parts.append(
            "It exposes CLI "
            + ", ".join(f["cli_commands"][:15] + ["--" + x for x in f["cli_flags"][:20]])
            + "."
        )
    if f["entrypoints"]:
        parts.append("It is a runnable script entrypoint.")
    if f["imports"]:
        parts.append("It imports " + ", ".join(f["imports"][:20]) + ".")
    if f["paired_test"]:
        parts.append(f"Related test: {f['paired_test']}.")
    if f["domain_terms"]:
        parts.append("Domain terms: " + ", ".join(f["domain_terms"]) + ".")
    return " ".join(parts)


def build_card(path: str, symbols_by_kind: Dict[str, List[str]], fmt: str = "code_shaped") -> CardSpec:
    """Build a deterministic file card in the requested format."""
    f = build_card_fields(path, symbols_by_kind)
    card_text = _render_prose(f) if fmt == "prose" else _render_code_shaped(f)
    digest = hashlib.sha256(
        f"{CARD_EXTRACTOR_VERSION}|{fmt}|{card_text}".encode("utf-8", errors="ignore")
    ).hexdigest()
    return CardSpec(
        path=path,
        surface=f["surface"],
        file_role=f["role"],
        card_text=card_text,
        card_text_format=fmt,
        card_hash=digest,
        domain_terms=f["domain_terms"],
    )
