"""Deterministic graph edge extraction from source files."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Set


@dataclass
class ExtractedEdge:
    rel_type: str
    from_path: str
    to_ref: str
    confidence: float
    provenance: str
    inferred: bool = False


def extract_calls(content: str, rel_path: str) -> List[ExtractedEdge]:
    """Extract CALLS edges from function/method invocations."""
    edges: List[ExtractedEdge] = []
    seen: Set[str] = set()
    patterns = [
        r"\b([a-zA-Z_][\w]*)\s*\(",
        r"\.\s*([a-zA-Z_][\w]*)\s*\(",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, content):
            callee = match.group(1)
            if callee in {"if", "for", "while", "return", "def", "class", "import", "print", "len"}:
                continue
            key = callee
            if key in seen:
                continue
            seen.add(key)
            edges.append(
                ExtractedEdge(
                    rel_type="CALLS",
                    from_path=rel_path,
                    to_ref=callee,
                    confidence=0.75,
                    provenance=f"regex:call:{pattern}",
                    inferred=True,
                )
            )
    return edges[:40]


def extract_uses(content: str, rel_path: str, ext: str) -> List[ExtractedEdge]:
    """Extract USES edges from imports and type references."""
    edges: List[ExtractedEdge] = []
    if ext == ".py":
        for match in re.finditer(r"\b([A-Z][a-zA-Z0-9_]+)\b", content):
            name = match.group(1)
            if name in {"True", "False", "None", "Dict", "List", "Optional", "Any"}:
                continue
            edges.append(
                ExtractedEdge(
                    rel_type="USES",
                    from_path=rel_path,
                    to_ref=name,
                    confidence=0.65,
                    provenance="regex:type_ref",
                    inferred=True,
                )
            )
    return edges[:30]


def extract_tested_by(rel_path: str, all_paths: List[str]) -> List[ExtractedEdge]:
    """Map source files to test files by stem convention."""
    edges: List[ExtractedEdge] = []
    lower = rel_path.lower()
    if "test" in lower:
        return edges
    stem = rel_path.rsplit("/", 1)[-1]
    base = stem.rsplit(".", 1)[0]
    for candidate in all_paths:
        cand_lower = candidate.lower()
        if "test" not in cand_lower:
            continue
        cand_stem = candidate.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        if cand_stem.replace("test_", "").replace("_test", "") == base:
            edges.append(
                ExtractedEdge(
                    rel_type="TESTED_BY",
                    from_path=rel_path,
                    to_ref=candidate,
                    confidence=0.9,
                    provenance="convention:stem_match",
                    inferred=False,
                )
            )
    return edges


def extract_tab_core_links(rel_path: str, all_paths: List[str]) -> List[ExtractedEdge]:
    """Link extension tab components to shared options/core module (tab↔core pattern)."""
    normalized = rel_path.replace("\\", "/").lower()
    edges: List[ExtractedEdge] = []

    core_candidates = [
        p for p in all_paths
        if p.replace("\\", "/").lower().endswith("extension/src/options/core.ts")
        or p.replace("\\", "/").lower().endswith("options/core.ts")
    ]
    if not core_candidates:
        return edges
    core_path = core_candidates[0]

    if "/options/tabs/" in normalized and normalized.endswith((".tsx", ".ts")):
        edges.append(
            ExtractedEdge(
                rel_type="RELATED_TO",
                from_path=rel_path,
                to_ref=core_path,
                confidence=0.88,
                provenance="convention:tab_core",
                inferred=False,
            )
        )
        edges.append(
            ExtractedEdge(
                rel_type="RELATED_TO",
                from_path=core_path,
                to_ref=rel_path,
                confidence=0.88,
                provenance="convention:core_tab",
                inferred=False,
            )
        )
    elif normalized.endswith("options/core.ts") or normalized.endswith("extension/src/options/core.ts"):
        for candidate in all_paths:
            cand_norm = candidate.replace("\\", "/").lower()
            if "/options/tabs/" not in cand_norm or not cand_norm.endswith((".tsx", ".ts")):
                continue
            edges.append(
                ExtractedEdge(
                    rel_type="RELATED_TO",
                    from_path=rel_path,
                    to_ref=candidate,
                    confidence=0.88,
                    provenance="convention:core_tab",
                    inferred=False,
                )
            )
    return edges


def retrieval_tab_core_boost(
    path: str,
    top_paths: List[str],
    task_description: str,
) -> float:
    """Runtime boost for core.ts when tab files rank highly (no re-index required)."""
    norm = path.replace("\\", "/").lower()
    if not (norm.endswith("options/core.ts") or norm.endswith("extension/src/options/core.ts")):
        return 0.0
    lower = task_description.lower()
    if not any(t in lower for t in ("extension", "options", "tab", "chrome", "popup")):
        return 0.0
    tab_hits = sum(1 for p in top_paths[:15] if "/options/tabs/" in p.replace("\\", "/").lower())
    return min(0.25 + tab_hits * 0.12, 0.65)


_DOMAIN_STOP = {
    "test", "tests", "the", "and", "for", "with", "from", "main", "run", "py",
    "util", "utils", "helper", "helpers", "common", "core", "base", "init",
}


def _domain_tokens(rel_path: str) -> set:
    stem = rel_path.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
    parts = re.split(r"[_\-.]+", stem.lower())
    return {p for p in parts if len(p) >= 3 and p not in _DOMAIN_STOP}


def extract_route_edges(content: str, rel_path: str, ext: str) -> List[ExtractedEdge]:
    """ROUTE -> HANDLER_FILE: FastAPI route declarations handled by this file."""
    if ext != ".py":
        return []
    edges: List[ExtractedEdge] = []
    prefix = ""
    pm = re.search(r"APIRouter\([^)]*prefix\s*=\s*[\"']([^\"']+)[\"']", content)
    if pm:
        prefix = pm.group(1).rstrip("/")
    for m in re.finditer(
        r"@(?:app|router|api_router|[a-z_]+_router)\.(get|post|put|delete|patch|websocket)"
        r"\(\s*[\"']([^\"']+)[\"']",
        content,
    ):
        path = m.group(2)
        full = (prefix + path) if (prefix and path.startswith("/")) else path
        edges.append(
            ExtractedEdge(
                rel_type="ROUTE",
                from_path=rel_path,
                to_ref=full,
                confidence=0.9,
                provenance=f"regex:route:{m.group(1).upper()}",
                inferred=False,
            )
        )
    return edges[:40]


def extract_import_file_edges(
    content: str, rel_path: str, ext: str, all_paths: List[str]
) -> List[ExtractedEdge]:
    """IMPORTS / TEST->SOURCE edges resolved to concrete repo files.

    Test files additionally emit a TESTS edge to each imported source file — the
    non-stem TEST->SOURCE link the convention-based ``extract_tested_by`` cannot infer.
    """
    from brain.indexers.symbol_extractors import resolve_imports_to_paths

    edges: List[ExtractedEdge] = []
    is_test = "test" in rel_path.replace("\\", "/").lower()
    for target in resolve_imports_to_paths(content, ext, rel_path, all_paths):
        edges.append(
            ExtractedEdge(
                rel_type="IMPORTS",
                from_path=rel_path,
                to_ref=target,
                confidence=0.85,
                provenance="regex:import_resolved",
                inferred=False,
            )
        )
        if is_test and "test" not in target.replace("\\", "/").lower():
            edges.append(
                ExtractedEdge(
                    rel_type="TESTS",
                    from_path=rel_path,
                    to_ref=target,
                    confidence=0.8,
                    provenance="regex:test_imports_source",
                    inferred=False,
                )
            )
    return edges


def extract_script_domain_edges(rel_path: str, all_paths: List[str]) -> List[ExtractedEdge]:
    """SCRIPT -> DOMAIN: link sibling scripts sharing >=2 domain stem tokens."""
    norm = rel_path.replace("\\", "/").lower()
    if not norm.startswith("scripts/") or "test" in norm:
        return []
    tokens = _domain_tokens(rel_path)
    if len(tokens) < 2:
        return []
    edges: List[ExtractedEdge] = []
    for cand in all_paths:
        cn = cand.replace("\\", "/").lower()
        if cand == rel_path or not cn.startswith("scripts/") or "test" in cn:
            continue
        shared = tokens & _domain_tokens(cand)
        if len(shared) >= 2:
            edges.append(
                ExtractedEdge(
                    rel_type="SCRIPT_DOMAIN",
                    from_path=rel_path,
                    to_ref=cand,
                    confidence=0.6,
                    provenance=f"domain:{'+'.join(sorted(shared))}",
                    inferred=True,
                )
            )
    return edges[:10]


def extract_affects(rel_path: str, feature_patterns: Dict[str, List[str]]) -> List[ExtractedEdge]:
    """Link files to features they affect via feature_map patterns."""
    import fnmatch

    edges: List[ExtractedEdge] = []
    for feat_name, patterns in feature_patterns.items():
        for pattern in patterns:
            if fnmatch.fnmatch(rel_path, pattern) or fnmatch.fnmatch(rel_path, f"*{pattern}*"):
                edges.append(
                    ExtractedEdge(
                        rel_type="AFFECTS",
                        from_path=rel_path,
                        to_ref=feat_name,
                        confidence=0.95,
                        provenance=f"feature_map:{pattern}",
                        inferred=False,
                    )
                )
                break
    return edges
