import re
import os
import yaml
from typing import List, Optional, Any, Dict
from sqlalchemy import select
from brain.database.session import async_session_factory
from brain.database.models import Rule
from brain.memory.repo_scope import normalize_repo_scope, repository_scope_clause

def slugify(text: str) -> str:
    """Helper to convert a rule name to a standard slug ID."""
    text = text.lower()
    text = re.sub(r'[^a-z0-9\-]', '-', text)
    text = re.sub(r'-+', '-', text)
    return text.strip('-')

class RuleStore:
    """Store for managing compliance and development rules."""

    @classmethod
    async def add_rule(
        cls,
        name: str,
        repo_path: Optional[str] = None,
        description: Optional[str] = None,
        type: Optional[str] = None,
        severity: Optional[str] = None,
        status: Optional[str] = "active",
        applies_to: Optional[Dict[str, Any]] = None,
        rule_id: Optional[str] = None,
    ) -> str:
        """Saves or updates a rule in Postgres and returns its id."""
        r_id = rule_id or slugify(name)
        normalized_repo_path = normalize_repo_scope(repo_path)
        async with async_session_factory() as session:
            result = await session.execute(select(Rule).where(Rule.id == r_id))
            existing = result.scalar_one_or_none()
            if existing:
                existing.name = name
                existing.repo_path = normalized_repo_path
                existing.description = description
                existing.type = type
                existing.severity = severity
                existing.status = status
                existing.applies_to = applies_to
            else:
                new_rule = Rule(
                    id=r_id,
                    name=name,
                    repo_path=normalized_repo_path,
                    description=description,
                    type=type,
                    severity=severity,
                    status=status,
                    applies_to=applies_to
                )
                session.add(new_rule)
            await session.commit()
            return r_id

    @classmethod
    async def list_rules(cls) -> List[Rule]:
        """Retrieves all rules."""
        async with async_session_factory() as session:
            result = await session.execute(select(Rule).order_by(Rule.id))
            return list(result.scalars().all())

    @classmethod
    async def list_active_rules(cls, repo_path: Optional[str] = None) -> List[Rule]:
        """Return active global rules plus rules owned by one repository."""
        async with async_session_factory() as session:
            result = await session.execute(
                select(Rule).where(
                    Rule.status == "active",
                    repository_scope_clause(Rule, repo_path),
                )
            )
            return list(result.scalars().all())

    @classmethod
    async def sync_rules_from_yaml(cls, yaml_path: str) -> None:
        """Reads a YAML file and synchronizes the rules in the Postgres database."""
        if not os.path.exists(yaml_path):
            raise FileNotFoundError(f"YAML path {yaml_path} does not exist.")

        with open(yaml_path, "r", encoding="utf-8") as f:
            content = yaml.safe_load(f)

        rules_list = []
        if isinstance(content, dict):
            if "rules" in content and isinstance(content["rules"], list):
                rules_list = content["rules"]
            else:
                # Handles dict map format: rule_id -> properties
                for r_id, r_val in content.items():
                    if isinstance(r_val, dict):
                        if "id" not in r_val:
                            r_val["id"] = r_id
                        rules_list.append(r_val)
        elif isinstance(content, list):
            rules_list = content

        async with async_session_factory() as session:
            for rule_data in rules_list:
                name = rule_data.get("name")
                if not name:
                    continue

                r_id = rule_data.get("id") or rule_data.get("rule_id") or slugify(name)
                description = rule_data.get("description")
                r_type = rule_data.get("type") or rule_data.get("rule_type")
                severity = rule_data.get("severity")
                status = rule_data.get("status", "active")
                applies_to = rule_data.get("applies_to")
                repo_path = normalize_repo_scope(rule_data.get("repo_path"))

                result = await session.execute(select(Rule).where(Rule.id == r_id))
                existing_rule = result.scalar_one_or_none()

                if existing_rule:
                    existing_rule.name = name
                    existing_rule.repo_path = repo_path
                    existing_rule.description = description
                    existing_rule.type = r_type
                    existing_rule.severity = severity
                    existing_rule.status = status
                    existing_rule.applies_to = applies_to
                else:
                    new_rule = Rule(
                        id=r_id,
                        name=name,
                        repo_path=repo_path,
                        description=description,
                        type=r_type,
                        severity=severity,
                        status=status,
                        applies_to=applies_to
                    )
                    session.add(new_rule)
            await session.commit()

# Module-level functions to match user request specifications
async def add_rule(
    name: str,
    repo_path: Optional[str] = None,
    description: Optional[str] = None,
    type: Optional[str] = None,
    severity: Optional[str] = None,
    status: Optional[str] = "active",
    applies_to: Optional[Dict[str, Any]] = None,
    rule_id: Optional[str] = None,
) -> str:
    return await RuleStore.add_rule(
        name=name,
        repo_path=repo_path,
        description=description,
        type=type,
        severity=severity,
        status=status,
        applies_to=applies_to,
        rule_id=rule_id,
    )

async def list_rules() -> List[Rule]:
    return await RuleStore.list_rules()


async def list_active_rules(repo_path: Optional[str] = None) -> List[Rule]:
    return await RuleStore.list_active_rules(repo_path)


async def sync_rules_from_yaml(yaml_path: str) -> None:
    await RuleStore.sync_rules_from_yaml(yaml_path)
