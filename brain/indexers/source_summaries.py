"""Deterministic, bounded descriptions available before any chat-model work."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SourceSummaries:
    file: str
    symbols: dict[tuple[str, int], str]


def source_summaries(content: str, extension: str, path: str, symbols: list[dict[str, Any]]) -> SourceSummaries:
    """Describe module intent and definitions without executing or sending code.

    Extraction/chunk boundaries stay with the existing parser. This stage only
    supplies prose fields; malformed or very large source uses bounded textual
    headers/signatures rather than making initial indexing depend on synthesis.
    """
    module_doc = ""
    docs: dict[tuple[str, int], str] = {}
    if extension == ".py" and len(content) <= 1_000_000:
        try:
            tree = ast.parse(content)
            module_doc = ast.get_docstring(tree) or ""
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    docs[(node.name, node.lineno)] = ast.get_docstring(node) or ""
        except (SyntaxError, ValueError, RecursionError):
            pass
    if not module_doc:
        headers: list[str] = []
        for line in content.splitlines()[:12]:
            if not line.strip():
                if headers:
                    break
                continue
            if extension == ".py" and not line.lstrip().startswith("#"):
                break
            headers.append(re.sub(r"^\s*(?:#+|//|/\*+|\*+)\s*", "", line).strip())
            if len(headers) == 4:
                break
        module_doc = " ".join(headers)
    descriptions: dict[tuple[str, int], str] = {}
    signatures: list[str] = []
    for symbol in symbols:
        name = str(symbol["name"])
        signature = str(symbol.get("signature") or name)
        key = (name, int(symbol.get("start_line") or 0))
        doc = docs.get(key, "")
        descriptions[key] = (signature + (" — " + re.sub(r"\s+", " ", doc) if doc else ""))[:1600]
        if signature not in signatures and len(signatures) < 12:
            signatures.append(signature[:160])
    summary = f"File {path} with {len(content.splitlines())} lines."
    if module_doc:
        summary += " " + re.sub(r"\s+", " ", module_doc)[:1600]
    if signatures:
        summary += " Definitions: " + "; ".join(signatures)
    return SourceSummaries(summary[:3200], descriptions)
