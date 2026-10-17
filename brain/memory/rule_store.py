import re
import os
import hashlib
import json
import yaml
from typing import List, Optional, Any, Dict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from brain.database.session import async_session_factory
from brain.database.models import Rule
from brain.memory.provenance import MemoryWriteResult, check_expected_version
from brain.memory.repo_scope import normalize_repo_scope, repository_scope_clause, scope_equals

# Content fields that take part in change detection and are copied into history rows.
_RULE_CONTENT_FIELDS = ("name", "repo_path", "description", "type", "severity", "status", "applies_to")


def slugify(text: str) -> str:
    """Helper to convert a rule name to a standard slug ID."""
    text = text.lower()
    text = re.sub(r'[^a-z0-9\-]', '-', text)
    text = re.sub(r'-+', '-', text)
    return text.strip('-')


def scoped_rule_id(rule_key: str, repo_path: Optional[str]) -> str:
    scope = normalize_repo_scope(repo_path)
    identity = json.dumps([scope, rule_key], ensure_ascii=False).encode()
    return f"{rule_key[:221]}--{hashlib.sha256(identity).hexdigest()[:32]}"


def rule_history_id(rule_id: str, version: int) -> str:
    """Primary key of the history row that keeps content version ``version`` of ``rule_id``."""
    digest = hashlib.sha256(f"{rule_id}|{version}".encode("utf-8")).hexdigest()[:20]
    return f"{rule_id[:200]}--h{digest}"


async def _find_rule(session, rule_key: str, scope: Optional[str]):
    scoped_id = scoped_rule_id(rule_key, scope)
    # Preserve existing bare IDs in their original scope. A matching ID in
    # another repository is never re-parented or overwritten. History rows
    # (superseded content copies) are never the canonical match.
    result = await session.execute(select(Rule).where(
        Rule.id.in_([rule_key, scoped_id]), scope_equals(Rule.repo_path, scope),
        Rule.superseded_by_id.is_(None),
    ).order_by(Rule.id))
    return result.scalar_one_or_none(), scoped_id


def _supersede_rule_content(session, existing: Rule, requested: Dict[str, Any],
                            principal_id: Optional[int]) -> Optional[str]:
    """Apply ``requested`` content to the canonical rule, keeping the old content as history.

    Returns the history row id when the content changed, None when it was identical
    (no new version). Mirrors ``DecisionStore.record_decision``: the canonical id is
    stable, the previous content lives on in a ``superseded`` row."""
    if all(getattr(existing, field) == value for field, value in requested.items()):
        return None
    current_version = existing.version or 1
    history = Rule(
        id=rule_history_id(existing.id, current_version),
        superseded_by_id=existing.id,
        supersedes_id=existing.supersedes_id,
        version=current_version,
        author_principal_id=existing.author_principal_id,
        updated_by_principal_id=existing.updated_by_principal_id,
        created_at=existing.created_at,
        **{field: getattr(existing, field) for field in _RULE_CONTENT_FIELDS if field != "status"},
        status="superseded",
    )
    session.add(history)
    for field, value in requested.items():
        setattr(existing, field, value)
    # Authorship stays as recorded: legacy rows keep NULL (no adoption on edit).
    existing.version = current_version + 1
    existing.supersedes_id = history.id
    existing.updated_by_principal_id = principal_id
    return history.id


class RuleStore:
    """Store for managing compliance and development rules."""

    @classmethod
    async def record_rule(
        cls,
        name: str,
        repo_path: Optional[str] = None,
        description: Optional[str] = None,
        type: Optional[str] = None,
        severity: Optional[str] = None,
        status: Optional[str] = "active",
        applies_to: Optional[Dict[str, Any]] = None,
        rule_id: Optional[str] = None,
        author_principal_id: Optional[int] = None,
        expected_version: Optional[int] = None,
        session: Optional[AsyncSession] = None,
    ) -> MemoryWriteResult:
        """Create the canonical rule for this key+scope, or supersede its content.

        A content change never overwrites in place: the previous content is kept in a
        history row (``status="superseded"``, ``superseded_by_id`` -> canonical id), the
        canonical row's ``version`` is bumped and ``updated_by_principal_id`` names the
        principal behind the change. The first author is kept. ``expected_version`` is
        compare-and-swap (0 = must not exist yet) and raises ``MemoryVersionConflict``
        on a mismatch. With ``session`` the write is only flushed inside the caller's
        transaction (proposal approval commits it together with the proposal)."""
        r_id = rule_id or slugify(name)
        normalized_repo_path = normalize_repo_scope(repo_path)
        requested = {
            "name": name,
            "repo_path": normalized_repo_path,
            "description": description,
            "type": type,
            "severity": severity,
            "status": status,
            "applies_to": applies_to,
        }
        async def _write(sess: AsyncSession) -> MemoryWriteResult:
            existing, scoped_id = await _find_rule(sess, r_id, normalized_repo_path)
            check_expected_version("rule", r_id, expected_version,
                                   existing.version if existing is not None else None)
            if existing:
                history_id = _supersede_rule_content(sess, existing, requested, author_principal_id)
                await sess.flush()
                return MemoryWriteResult(id=existing.id, version=existing.version or 1, created=False,
                                         changed=history_id is not None, superseded_id=history_id)
            sess.add(Rule(
                id=scoped_id,
                author_principal_id=author_principal_id,
                updated_by_principal_id=author_principal_id,
                version=1,
                **requested,
            ))
            await sess.flush()
            return MemoryWriteResult(id=scoped_id, version=1, created=True, changed=True)

        if session is not None:
            return await _write(session)
        async with async_session_factory() as own_session:
            written = await _write(own_session)
            await own_session.commit()
            return written

    @classmethod
    async def add_rule(cls, *args: Any, **kwargs: Any) -> str:
        """``record_rule`` for callers that only need the canonical id."""
        return str((await cls.record_rule(*args, **kwargs)).id)

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
                requested = {
                    "name": name,
                    "repo_path": normalize_repo_scope(rule_data.get("repo_path")),
                    "description": rule_data.get("description"),
                    "type": rule_data.get("type") or rule_data.get("rule_type"),
                    "severity": rule_data.get("severity"),
                    "status": rule_data.get("status", "active"),
                    "applies_to": rule_data.get("applies_to"),
                }

                existing_rule, scoped_id = await _find_rule(session, r_id, requested["repo_path"])

                if existing_rule:
                    # YAML sync is the operator's own file: no principal, but still versioned.
                    _supersede_rule_content(session, existing_rule, requested, None)
                else:
                    session.add(Rule(id=scoped_id, version=1, **requested))
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
    author_principal_id: Optional[int] = None,
    expected_version: Optional[int] = None,
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
        author_principal_id=author_principal_id,
        expected_version=expected_version,
    )

async def list_rules() -> List[Rule]:
    return await RuleStore.list_rules()


async def list_active_rules(repo_path: Optional[str] = None) -> List[Rule]:
    return await RuleStore.list_active_rules(repo_path)


async def sync_rules_from_yaml(yaml_path: str) -> None:
    await RuleStore.sync_rules_from_yaml(yaml_path)
