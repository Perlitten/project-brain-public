import os
import yaml
from typing import Dict, Any, List, Optional
from brain.memory.decision_store import DecisionStore
from brain.memory.rule_store import RuleStore

class ObsidianIntegration:
    """Scan and parse Obsidian vaults for project rules and architectural decisions."""

    def __init__(self, vault_dir: str):
        self.vault_dir = vault_dir

    def scan_vault(self) -> List[Dict[str, Any]]:
        """Scans the Obsidian vault recursively for markdown files and parses frontmatter."""
        parsed_items = []
        if not os.path.exists(self.vault_dir):
            raise FileNotFoundError(f"Obsidian vault directory not found: {self.vault_dir}")

        for root, _, files in os.walk(self.vault_dir):
            for file in files:
                if file.endswith(".md"):
                    file_path = os.path.join(root, file)
                    item_data = self._parse_file(file_path)
                    if item_data:
                        parsed_items.append(item_data)
        return parsed_items

    def _parse_file(self, file_path: str) -> Optional[Dict[str, Any]]:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception:
            return None

        # Obsidian frontmatter must start with triple-dashes at the top
        if not content.startswith("---"):
            return None

        parts = content.split("---", 2)
        if len(parts) < 3:
            return None

        frontmatter_str = parts[1]
        body = parts[2].strip()

        try:
            data = yaml.safe_load(frontmatter_str)
        except Exception:
            return None

        if not isinstance(data, dict):
            return None

        # Store helper metadata
        data["_file_path"] = file_path
        data["_body"] = body

        # Deduce a title/name fallback from file name
        filename_no_ext = os.path.splitext(os.path.basename(file_path))[0]
        data["_filename"] = filename_no_ext

        return data

    async def import_to_stores(self) -> Dict[str, List[Any]]:
        """Imports decisions and rules into the SQL database.

        Returns:
            A dict containing lists of the imported decision IDs and rule IDs.
        """
        items = self.scan_vault()
        imported_decisions = []
        imported_rules = []

        for item in items:
            # Detect whether it is a decision or a rule
            item_type = item.get("type", "").lower()

            if not item_type:
                # Deduce type by analyzing frontmatter keys
                if any(k in item for k in ["reason", "consequences", "affected_features"]):
                    item_type = "decision"
                elif any(k in item for k in ["severity", "rule_type", "applies_to"]):
                    item_type = "rule"

            if item_type == "decision":
                title = str(item.get("title") or item.get("_filename") or "")
                description = item.get("description") or item.get("_body")
                status = item.get("status") or "active"
                date_val = item.get("date")
                reason = item.get("reason")
                consequences = item.get("consequences")
                affected_features = item.get("affected_features") or []
                affected_modules = item.get("affected_modules") or []
                affected_files = item.get("affected_files") or []

                decision_id = await DecisionStore.add_decision(
                    title=title,
                    repo_path=item.get("repo_path"),
                    description=description,
                    status=status,
                    date=date_val,
                    reason=reason,
                    consequences=consequences,
                    affected_features=affected_features,
                    affected_modules=affected_modules,
                    affected_files=affected_files
                )
                imported_decisions.append(decision_id)

            elif item_type == "rule":
                name = str(item.get("name") or item.get("title") or item.get("_filename") or "")
                description = item.get("description") or item.get("_body")
                rule_type = item.get("rule_type") or item.get("type")
                if rule_type == "rule":
                    rule_type = "architecture"  # default standard

                severity = item.get("severity") or "medium"
                status = item.get("status") or "active"
                applies_to = item.get("applies_to") or {}
                rule_id = item.get("id") or item.get("rule_id")

                imported_id = await RuleStore.add_rule(
                    name=name,
                    repo_path=item.get("repo_path"),
                    description=description,
                    type=rule_type,
                    severity=severity,
                    status=status,
                    applies_to=applies_to,
                    rule_id=rule_id
                )
                imported_rules.append(imported_id)

        return {
            "decisions": imported_decisions,
            "rules": imported_rules
        }
