"""Retrieval v6 P1 structural symbol extraction.

Deterministic regex extractors for CLI/route/script/import symbols. Kept separate
from `extract_symbols_from_content` (which drives chunk boundaries) so that adding
these symbols never shifts chunks/embeddings — the vector channel stays byte-identical
to the frozen v5 index after a symbol backfill.

All extractors return Symbol-shaped dicts: {name, kind, signature, start_line, end_line}.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from brain.search.surfaces import IMPORT_TARGET_KIND

# --- regexes (compiled once) --------------------------------------------------
_ROUTE_RE = re.compile(
    r"@(?:app|router|api_router|[a-z_]+_router)\.(get|post|put|delete|patch|websocket)"
    r"\(\s*[\"']([^\"']+)[\"']"
)
_PREFIX_RE = re.compile(r"APIRouter\([^)]*prefix\s*=\s*[\"']([^\"']+)[\"']")
_ADD_ARG_RE = re.compile(r"add_argument\(\s*[\"'](--?[A-Za-z0-9][\w-]*)[\"']")
_CLICK_CMD_RE = re.compile(r"@(?:app|cli|click|[a-z_]+_app)\.command\(\s*(?:[\"']([\w-]+)[\"'])?")
_MAIN_RE = re.compile(r"if\s+__name__\s*==\s*[\"']__main__[\"']")
_PY_IMPORT_FROM_RE = re.compile(r"^\s*from\s+([A-Za-z0-9_.]+)\s+import\b", re.MULTILINE)
_PY_IMPORT_RE = re.compile(r"^\s*import\s+([A-Za-z0-9_.]+)", re.MULTILINE)
_JS_IMPORT_RE = re.compile(r"""(?:from|require\(\s*)[\"']([^\"']+)[\"']""")


def _stem(rel_path: str) -> str:
    name = rel_path.replace("\\", "/").rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0]


def extract_structural_symbols(content: str, ext: str, rel_path: str) -> List[Dict[str, Any]]:
    """Extract CLI/route/script-entrypoint symbols (v6 symbol channel, kind-gated)."""
    out: List[Dict[str, Any]] = []
    if ext != ".py":
        return out
    lines = content.splitlines()
    prefix = ""
    m = _PREFIX_RE.search(content)
    if m:
        prefix = m.group(1).rstrip("/")

    for i, line in enumerate(lines, 1):
        rm = _ROUTE_RE.search(line)
        if rm:
            method, path = rm.group(1).upper(), rm.group(2)
            full = (prefix + path) if (prefix and path.startswith("/")) else path
            out.append({
                "name": full,
                "kind": "api_route",
                "signature": f"{method} {full}",
                "start_line": i,
                "end_line": i,
            })
            continue
        am = _ADD_ARG_RE.search(line)
        if am:
            flag = am.group(1).lstrip("-")
            out.append({
                "name": flag,
                "kind": "cli_flag",
                "signature": am.group(1),
                "start_line": i,
                "end_line": i,
            })
        cm = _CLICK_CMD_RE.search(line)
        if cm:
            # command name from decorator arg, else the def on the next non-blank line
            name = cm.group(1)
            if not name:
                for j in range(i, min(i + 3, len(lines))):
                    dm = re.match(r"\s*(?:async\s+)?def\s+([A-Za-z0-9_]+)", lines[j])
                    if dm:
                        name = dm.group(1)
                        break
            if name:
                out.append({
                    "name": name,
                    "kind": "cli_command",
                    "signature": line.strip(),
                    "start_line": i,
                    "end_line": i,
                })

    if _MAIN_RE.search(content):
        out.append({
            "name": _stem(rel_path),
            "kind": "script_entrypoint",
            "signature": 'if __name__ == "__main__"',
            "start_line": 1,
            "end_line": len(lines),
        })
    # de-dup by (name, kind)
    seen = set()
    deduped: List[Dict[str, Any]] = []
    for s in out:
        key = (s["name"], s["kind"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(s)
    return deduped


def resolve_imports_to_paths(content: str, ext: str, rel_path: str, all_paths: List[str]) -> List[str]:
    """Resolve a file's imports to repo-relative paths of imported files.

    Deterministic stem+subpath matching (same convention the graph IMPORTS pass uses),
    returning concrete indexed file paths so they can be stored as ``import_target``
    symbols for v6 co-rank injection.
    """
    if ext == ".py":
        modules = _PY_IMPORT_FROM_RE.findall(content) + _PY_IMPORT_RE.findall(content)
    elif ext in {".ts", ".tsx", ".js", ".jsx"}:
        modules = _JS_IMPORT_RE.findall(content)
    else:
        return []

    by_stem: Dict[str, List[str]] = {}
    for p in all_paths:
        by_stem.setdefault(_stem(p), []).append(p)

    targets: List[str] = []
    seen = set()
    for mod in modules:
        cleaned = mod.replace(".", "/").strip("/")
        if not cleaned or cleaned == rel_path:
            continue
        name = cleaned.split("/")[-1]
        # Prefer a path whose stem matches and whose subpath is consistent.
        for cand in by_stem.get(name, []):
            if cand == rel_path or cand in seen:
                continue
            cand_norm = cand.replace("\\", "/")
            if cleaned in cand_norm or "/" not in cleaned:
                targets.append(cand)
                seen.add(cand)
                break
    return targets[:25]


def extract_import_target_symbols(content: str, ext: str, rel_path: str, all_paths: List[str]) -> List[Dict[str, Any]]:
    """import_target symbols (name = resolved repo path). Co-rank injection only."""
    out: List[Dict[str, Any]] = []
    for target in resolve_imports_to_paths(content, ext, rel_path, all_paths):
        out.append({
            "name": target,
            "kind": IMPORT_TARGET_KIND,
            "signature": f"imports {target}",
            "start_line": 1,
            "end_line": 1,
        })
    return out
