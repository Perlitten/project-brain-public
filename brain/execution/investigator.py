"""Investigation Phase Tool Scoping and Confirmation for Project Brain v0.5.2."""

from __future__ import annotations

from typing import List


class InvestigationScopeGuard:
    """Enforces investigation-only tool permissions and evidence confirmation classification."""

    INVESTIGATION_ALLOWED_TOOLS = {
        "view_file",
        "grep_search",
        "list_dir",
        "brain_context_pack",
        "read_resource",
        "search_code",
    }

    WRITE_PROHIBITED_TOOLS = {
        "replace_file_content",
        "multi_replace_file_content",
        "write_to_file",
        "apply_patch",
        "run_command",
    }

    @classmethod
    def validate_tool_access(cls, phase: str, tool_name: str) -> bool:
        if phase in ("investigating", "planning"):
            if tool_name in cls.WRITE_PROHIBITED_TOOLS:
                raise PermissionError(f"Tool '{tool_name}' is forbidden during '{phase}' phase.")
        return True

    @staticmethod
    def classify_evidence_item(item_file: str, existing_files: List[str]) -> str:
        if item_file in existing_files:
            return "confirmed_in_source"
        return "not_found"
