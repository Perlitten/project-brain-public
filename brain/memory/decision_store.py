import datetime
from typing import List, Optional, Any
from sqlalchemy import func, or_, select
from brain.database.session import async_session_factory
from brain.database.models import Decision
from brain.memory.provenance import MemoryWriteResult, check_expected_version
from brain.memory.repo_scope import normalize_repo_scope, scope_equals


# Scope that carries the admin role for system_global knowledge writes. The legacy
# shared key and ``*`` credentials hold it; a plain ``core:write`` credential does not.
MEMORY_ADMIN_SCOPE = "core:admin"

# Content fields that take part in change detection and are copied into history rows.
_DECISION_CONTENT_FIELDS = (
    "description", "status", "date", "reason", "consequences",
    "affected_features", "affected_modules", "affected_files",
)


def caller_role_for(principal) -> str:
    """The memory role of a resolved principal: ``admin`` with ``core:admin``, else ``agent``."""
    return "admin" if principal.has_scope(MEMORY_ADMIN_SCOPE) else "agent"


def _coerce_date(date: Optional[Any]) -> Optional[datetime.date]:
    if not date:
        return None
    if isinstance(date, datetime.datetime):
        return date.date()
    if isinstance(date, datetime.date):
        return date
    if isinstance(date, str):
        try:
            return datetime.datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            return datetime.date.today()
    return None


class DecisionStore:
    """Store for managing architectural and technical decisions."""

    @classmethod
    async def record_decision(
        cls,
        title: str,
        repo_path: Optional[str] = None,
        description: Optional[str] = None,
        status: Optional[str] = "active",
        date: Optional[Any] = None,
        reason: Optional[str] = None,
        consequences: Optional[str] = None,
        affected_features: Optional[List[str]] = None,
        affected_modules: Optional[List[str]] = None,
        affected_files: Optional[List[str]] = None,
        caller_role: str = "agent",
        author_principal_id: Optional[int] = None,
        expected_version: Optional[int] = None,
    ) -> MemoryWriteResult:
        """Create the canonical decision for this title+scope, or supersede its content.

        ``caller_role`` is derived by the caller from its authenticated principal
        (``caller_role_for``) or is ``admin`` for the operator CLI; only the admin
        role may write ``system_global``.

        Nothing is overwritten silently. A content change copies the previous
        content into a history row (``status="superseded"``, ``superseded_by_id``
        pointing at the canonical row), bumps the canonical row's ``version``,
        links it through ``supersedes_id`` and records ``updated_by_principal_id``.
        The canonical id is stable, so references keep working. An identical
        re-post changes nothing and keeps the version.

        ``expected_version`` is compare-and-swap: pass the version last read (0
        for "must not exist yet") and the write raises ``MemoryVersionConflict``
        when someone else changed the decision first. ``author_principal_id`` is
        kept from the first record; every later change names its principal."""
        normalized_title = title.strip()
        if not normalized_title:
            raise ValueError("Decision title must not be empty")

        normalized_repo_path = normalize_repo_scope(repo_path)
        if not normalized_repo_path or normalized_repo_path == "system_global":
            if caller_role != "admin":
                raise PermissionError("Creating or updating system_global decisions requires elevated admin authorization.")
            normalized_repo_path = "system_global"

        requested = {
            "description": description,
            "status": status,
            "date": _coerce_date(date),
            "reason": reason,
            "consequences": consequences,
            "affected_features": affected_features,
            "affected_modules": affected_modules,
            "affected_files": affected_files,
        }

        async with async_session_factory() as session:
            result = await session.execute(
                select(Decision)
                .where(
                    func.lower(func.trim(Decision.title)) == normalized_title.casefold(),
                    scope_equals(Decision.repo_path, normalized_repo_path),
                    Decision.superseded_by_id.is_(None),
                )
                .order_by(Decision.id.desc())
                .limit(1)
            )
            existing = result.scalar_one_or_none()
            check_expected_version("decision", normalized_title, expected_version,
                                   existing.version if existing is not None else None)
            if existing is not None:
                changes = {
                    field: value for field, value in requested.items()
                    if value is not None and getattr(existing, field) != value
                }
                if existing.title != normalized_title:
                    changes["title"] = normalized_title
                if existing.author_principal_id is None:
                    existing.author_principal_id = author_principal_id
                if not changes:
                    await session.commit()
                    return MemoryWriteResult(id=existing.id, version=existing.version or 1,
                                             created=False, changed=False)
                history = Decision(
                    title=existing.title,
                    repo_path=existing.repo_path,
                    status="superseded",
                    superseded_by_id=existing.id,
                    author_principal_id=existing.author_principal_id,
                    updated_by_principal_id=existing.updated_by_principal_id,
                    supersedes_id=existing.supersedes_id,
                    version=existing.version or 1,
                    created_at=existing.created_at,
                    **{field: getattr(existing, field) for field in _DECISION_CONTENT_FIELDS if field != "status"},
                )
                session.add(history)
                await session.flush()
                for field, value in changes.items():
                    setattr(existing, field, value)
                existing.version = (existing.version or 1) + 1
                existing.supersedes_id = history.id
                existing.updated_by_principal_id = author_principal_id
                await session.commit()
                await session.refresh(existing)
                return MemoryWriteResult(id=existing.id, version=existing.version, created=False,
                                         changed=True, superseded_id=history.id)

            new_decision = Decision(
                title=normalized_title,
                repo_path=normalized_repo_path,
                description=description,
                status=status,
                date=requested["date"] or datetime.date.today(),
                reason=reason,
                consequences=consequences,
                affected_features=affected_features,
                affected_modules=affected_modules,
                affected_files=affected_files,
                author_principal_id=author_principal_id,
                updated_by_principal_id=author_principal_id,
                version=1,
            )
            session.add(new_decision)
            await session.commit()
            await session.refresh(new_decision)
            return MemoryWriteResult(id=new_decision.id, version=1, created=True, changed=True)

    @classmethod
    async def add_decision(cls, *args: Any, **kwargs: Any) -> int:
        """``record_decision`` for callers that only need the canonical id."""
        return int((await cls.record_decision(*args, **kwargs)).id)

    @classmethod
    async def list_decisions(cls) -> List[Decision]:
        """Retrieves all decisions."""
        async with async_session_factory() as session:
            result = await session.execute(select(Decision).order_by(Decision.id))
            return list(result.scalars().all())

    @classmethod
    async def search_decisions(cls, query: str) -> List[Decision]:
        """Searches decisions by title/description using SQLAlchemy LIKE/contains."""
        async with async_session_factory() as session:
            stmt = select(Decision).where(
                or_(Decision.title.ilike(f"%{query}%"), Decision.description.ilike(f"%{query}%"))
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    @classmethod
    async def deprecate_decision(cls, decision_id: int, principal_id: Optional[int] = None) -> bool:
        """Updates status to 'deprecated'."""
        async with async_session_factory() as session:
            result = await session.execute(select(Decision).where(Decision.id == decision_id))
            decision = result.scalar_one_or_none()
            if decision:
                decision.status = "deprecated"
                if principal_id is not None:
                    decision.updated_by_principal_id = principal_id
                await session.commit()
                return True
            return False


# Module-level functions to match user request specifications
async def add_decision(
    title: str,
    repo_path: Optional[str] = None,
    description: Optional[str] = None,
    status: Optional[str] = "active",
    date: Optional[Any] = None,
    reason: Optional[str] = None,
    consequences: Optional[str] = None,
    affected_features: Optional[List[str]] = None,
    affected_modules: Optional[List[str]] = None,
    affected_files: Optional[List[str]] = None,
) -> int:
    return await DecisionStore.add_decision(
        title=title,
        repo_path=repo_path,
        description=description,
        status=status,
        date=date,
        reason=reason,
        consequences=consequences,
        affected_features=affected_features,
        affected_modules=affected_modules,
        affected_files=affected_files,
    )


async def list_decisions() -> List[Decision]:
    return await DecisionStore.list_decisions()


async def search_decisions(query: str) -> List[Decision]:
    return await DecisionStore.search_decisions(query)


async def deprecate_decision(decision_id: int) -> bool:
    return await DecisionStore.deprecate_decision(decision_id)
