import datetime
from typing import List, Optional, Any
from sqlalchemy import func, or_, select
from brain.database.session import async_session_factory
from brain.database.models import Decision
from brain.memory.repo_scope import normalize_repo_scope


class DecisionStore:
    """Store for managing architectural and technical decisions."""

    @classmethod
    async def add_decision(
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
        auth_token: Optional[str] = None,
    ) -> int:
        """Create or update the canonical decision with this title."""
        normalized_title = title.strip()
        if not normalized_title:
            raise ValueError("Decision title must not be empty")

        normalized_repo_path = normalize_repo_scope(repo_path)
        if not normalized_repo_path or normalized_repo_path == "system_global":
            if caller_role != "admin" and not auth_token:
                raise PermissionError("Creating or updating system_global decisions requires elevated admin authorization.")
            normalized_repo_path = "system_global"

        actual_date = None
        if date:
            if isinstance(date, datetime.date):
                actual_date = date
            elif isinstance(date, datetime.datetime):
                actual_date = date.date()
            elif isinstance(date, str):
                try:
                    actual_date = datetime.datetime.strptime(date, "%Y-%m-%d").date()
                except ValueError:
                    actual_date = datetime.date.today()

        async with async_session_factory() as session:
            result = await session.execute(
                select(Decision)
                .where(
                    func.lower(func.trim(Decision.title)) == normalized_title.casefold(),
                    func.lower(func.trim(Decision.repo_path)) == normalized_repo_path.casefold(),
                )
                .order_by(Decision.id.desc())
                .limit(1)
            )
            existing = result.scalar_one_or_none()
            if existing is not None:
                existing.title = normalized_title
                if description is not None:
                    existing.description = description
                if status is not None:
                    existing.status = status
                if actual_date is not None:
                    existing.date = actual_date
                if reason is not None:
                    existing.reason = reason
                if consequences is not None:
                    existing.consequences = consequences
                if affected_features is not None:
                    existing.affected_features = affected_features
                if affected_modules is not None:
                    existing.affected_modules = affected_modules
                if affected_files is not None:
                    existing.affected_files = affected_files
                await session.commit()
                await session.refresh(existing)
                return existing.id

            new_decision = Decision(
                title=normalized_title,
                repo_path=normalized_repo_path,
                description=description,
                status=status,
                date=actual_date or datetime.date.today(),
                reason=reason,
                consequences=consequences,
                affected_features=affected_features,
                affected_modules=affected_modules,
                affected_files=affected_files,
            )
            session.add(new_decision)
            await session.commit()
            await session.refresh(new_decision)
            return new_decision.id

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
    async def deprecate_decision(cls, decision_id: int) -> bool:
        """Updates status to 'deprecated'."""
        async with async_session_factory() as session:
            result = await session.execute(select(Decision).where(Decision.id == decision_id))
            decision = result.scalar_one_or_none()
            if decision:
                decision.status = "deprecated"
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
