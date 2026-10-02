"""Architectural Drift Intelligence Analyzer v2 for Project Brain (Phases 2 & 3).

Executes AST-based static parsing using the declarative DriftRuleRegistry and computes
line-number-independent cryptographic fingerprints.
"""

from __future__ import annotations

import ast
import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, List, Optional, Set

from brain.insights.drift_rules import DriftRule, DriftRuleRegistry, get_default_rule_registry

# Ignored directory patterns
IGNORED_DIRS: Set[str] = {
    ".brain",
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
}

MAX_FILE_SIZE_BYTES = 2 * 1024 * 1024  # 2MB cap per file for safety


@dataclass(frozen=True)
class DriftFinding:
    """Stable architectural drift finding with line-number-independent fingerprint."""

    fingerprint: str
    rule_id: str
    rule_name: str
    file_path: str  # Normalized relative POSIX path
    line_number: int  # Kept in evidence, NOT in fingerprint!
    containing_symbol: str  # Enclosing function or class name, or 'global'
    imported_module: str
    severity: str
    description: str
    remediation_guidance: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def calculate_stable_fingerprint(
    rule_id: str,
    file_path: str,
    containing_symbol: str,
    imported_module: str,
) -> str:
    """Generate a stable line-number-independent SHA-256 fingerprint."""
    clean_path = file_path.replace("\\", "/").strip().lstrip("/")
    clean_symbol = containing_symbol.strip() or "global"
    clean_module = imported_module.strip()
    raw = f"{rule_id}:{clean_path}:{clean_symbol}:{clean_module}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return f"{rule_id.lower()}:{digest}"


class ArchitecturalDriftAnalyzer:
    """AST analyzer enforcing declarative rules and generating stable findings."""

    def __init__(self, repo_root: Path, registry: Optional[DriftRuleRegistry] = None):
        self.repo_root = repo_root.resolve()
        self.registry = registry or get_default_rule_registry()

    def analyze_file(self, relative_path: str, code_content: str) -> List[DriftFinding]:
        findings: List[DriftFinding] = []
        try:
            tree = ast.parse(code_content, filename=relative_path)
        except SyntaxError:
            return findings

        clean_path = relative_path.replace("\\", "/").strip().lstrip("/")
        active_rules = self.registry.list_active_rules()

        # Track containing symbol during AST walk
        self._traverse_node(tree, clean_path, "global", active_rules, findings)
        return findings

    def _traverse_node(
        self,
        node: ast.AST,
        file_path: str,
        current_symbol: str,
        rules: List[DriftRule],
        findings: List[DriftFinding],
    ) -> None:
        for child in ast.iter_child_nodes(node):
            next_symbol = current_symbol
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                next_symbol = child.name

            if isinstance(child, ast.Import):
                for alias in child.names:
                    self._evaluate_import(file_path, child.lineno, next_symbol, alias.name, rules, findings)
            elif isinstance(child, ast.ImportFrom):
                if child.module:
                    self._evaluate_import(file_path, child.lineno, next_symbol, child.module, rules, findings)

            self._traverse_node(child, file_path, next_symbol, rules, findings)

    def _evaluate_import(
        self,
        file_path: str,
        line: int,
        containing_symbol: str,
        imported_module: str,
        rules: List[DriftRule],
        findings: List[DriftFinding],
    ) -> None:
        for rule in rules:
            # Check source layer pattern match
            if not file_path.startswith(rule.source_layer_pattern.lstrip("/")):
                continue

            # Check forbidden import pattern match
            if rule.forbidden_import_pattern in imported_module:
                # Check exceptions
                if any(exc in file_path for exc in rule.allowed_exceptions):
                    continue

                fp = calculate_stable_fingerprint(rule.rule_id, file_path, containing_symbol, imported_module)
                finding = DriftFinding(
                    fingerprint=fp,
                    rule_id=rule.rule_id,
                    rule_name=rule.name,
                    file_path=file_path,
                    line_number=line,
                    containing_symbol=containing_symbol,
                    imported_module=imported_module,
                    severity=rule.severity,
                    description=rule.description,
                    remediation_guidance=rule.remediation_guidance,
                )
                findings.append(finding)


def scan_repository_drift(
    repo_root: Path,
    registry: Optional[DriftRuleRegistry] = None,
    max_file_size_bytes: int = MAX_FILE_SIZE_BYTES,
) -> List[DriftFinding]:
    analyzer = ArchitecturalDriftAnalyzer(repo_root, registry)
    all_findings: List[DriftFinding] = []

    for source_dir in ("brain", "apps"):
        target = repo_root / source_dir
        if not target.exists():
            continue
        for file_path in target.rglob("*.py"):
            # Check ignored directory patterns
            if any(part in IGNORED_DIRS for part in file_path.parts):
                continue
            if file_path.stat().st_size > max_file_size_bytes:
                continue

            try:
                rel = str(file_path.relative_to(repo_root))
                content = file_path.read_text(encoding="utf-8")
                findings = analyzer.analyze_file(rel, content)
                all_findings.extend(findings)
            except Exception:
                continue

    return all_findings
