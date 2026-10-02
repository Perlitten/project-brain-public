"""CODEOWNERS Parser and Ownership Resolver for Architectural Review Intelligence.

Discovers and parses standard CODEOWNERS rules using standard GitHub precedence:
1. .github/CODEOWNERS
2. CODEOWNERS
3. docs/CODEOWNERS
"""

from __future__ import annotations

import fnmatch
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


@dataclass
class CodeownerRule:
    pattern: str
    owners: List[str]
    line_number: int
    raw_line: str


@dataclass
class OwnershipDiagnostic:
    line_number: int
    raw_line: str
    message: str


@dataclass
class CodeownersFile:
    file_path: Optional[str]
    source_revision: str
    sha256: str
    rules: List[CodeownerRule]
    diagnostics: List[OwnershipDiagnostic]


@dataclass
class ResolvedOwnership:
    target_path: str
    owners: List[str]
    matching_rule: Optional[CodeownerRule] = None
    is_unowned: bool = False


class CodeownersParser:
    """Discovers and parses GitHub-compatible CODEOWNERS files."""

    LOCATIONS = [".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"]

    @classmethod
    def discover_and_parse(cls, repo_path: Path, revision: str = "HEAD") -> CodeownersFile:
        repo_path = repo_path.resolve()
        selected_file: Optional[Path] = None

        for loc in cls.LOCATIONS:
            p = repo_path / loc
            if p.exists() and p.is_file():
                selected_file = p
                break

        if not selected_file:
            return CodeownersFile(
                file_path=None,
                source_revision=revision,
                sha256="",
                rules=[],
                diagnostics=[],
            )

        content = selected_file.read_bytes()
        file_sha = hashlib.sha256(content).hexdigest()
        text = content.decode("utf-8", errors="replace")

        rules: List[CodeownerRule] = []
        diagnostics: List[OwnershipDiagnostic] = []

        for idx, line in enumerate(text.splitlines(), start=1):
            line_str = line.strip()
            if not line_str or line_str.startswith("#"):
                continue

            # Split pattern and owners
            parts = line_str.split()
            if len(parts) < 2:
                diagnostics.append(
                    OwnershipDiagnostic(
                        line_number=idx,
                        raw_line=line_str,
                        message="Missing owner string for pattern",
                    )
                )
                continue

            pattern = parts[0]
            owners = parts[1:]

            # Validate pattern
            if ".." in pattern or pattern.startswith("\\"):
                diagnostics.append(
                    OwnershipDiagnostic(
                        line_number=idx,
                        raw_line=line_str,
                        message="Invalid pattern: traversal or illegal character",
                    )
                )
                continue

            rules.append(
                CodeownerRule(
                    pattern=pattern,
                    owners=owners,
                    line_number=idx,
                    raw_line=line_str,
                )
            )

        rel_path = selected_file.relative_to(repo_path).as_posix()
        return CodeownersFile(
            file_path=rel_path,
            source_revision=revision,
            sha256=file_sha,
            rules=rules,
            diagnostics=diagnostics,
        )


class FindingOwnershipResolver:
    """Resolves owners for files and findings using parsed CODEOWNERS rules (last-matching-pattern-wins)."""

    def __init__(self, codeowners: CodeownersFile, default_owner: str = "@architecture-leads"):
        self.codeowners = codeowners
        self.default_owner = default_owner

    def _match_pattern(self, pattern: str, target_path: str) -> bool:
        norm_target = target_path.replace("\\", "/").lstrip("/")
        norm_pat = pattern.replace("\\", "/")

        # Directory pattern ends with /
        if norm_pat.endswith("/"):
            dir_pat = norm_pat.rstrip("/")
            if norm_target.startswith(dir_pat + "/") or norm_target == dir_pat:
                return True
            norm_pat = norm_pat + "*"

        # Root-anchored pattern starts with /
        if norm_pat.startswith("/"):
            norm_pat = norm_pat.lstrip("/")
            return fnmatch.fnmatch(norm_target, norm_pat) or fnmatch.fnmatch(norm_target, norm_pat + "/*")

        # Wildcard or simple pattern
        if fnmatch.fnmatch(norm_target, norm_pat) or fnmatch.fnmatch(norm_target, "*/" + norm_pat):
            return True
        if fnmatch.fnmatch(norm_target, norm_pat + "/*"):
            return True

        return False

    def resolve_path_ownership(self, target_path: str) -> ResolvedOwnership:
        norm_path = target_path.replace("\\", "/").lstrip("/")
        last_matching_rule: Optional[CodeownerRule] = None

        for rule in self.codeowners.rules:
            if self._match_pattern(rule.pattern, norm_path):
                last_matching_rule = rule

        if last_matching_rule:
            return ResolvedOwnership(
                target_path=norm_path,
                owners=last_matching_rule.owners,
                matching_rule=last_matching_rule,
                is_unowned=False,
            )

        return ResolvedOwnership(
            target_path=norm_path,
            owners=[self.default_owner],
            matching_rule=None,
            is_unowned=True,
        )
