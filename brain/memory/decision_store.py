import dataclasses
import datetime
from typing import List, Optional, Any, Tuple, cast
from sqlalchemy import CursorResult, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from brain.database.session import async_session_factory
from brain.database.models import Decision
from brain.memory.provenance import MemoryVersionConflict, MemoryWriteResult, check_expected_version
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


def decision_scope(repo_path: Optional[str]) -> str:
    """The stored scope of a decision: a normalized repository path, or ``system_global``."""
    normalized = normalize_repo_scope(repo_path)
    if not normalized or normalized == "system_global":
        return "system_global"
    return normalized


async def find_canonical_decision(session: AsyncSession, title: str, repo_path: Optional[str],
                                  lock: bool = False) -> Optional[Decision]:
    """The canonical (not superseded) decision for ``title`` in the scope of ``repo_path``, if any.

    ``lock`` takes the row lock (``SELECT ... FOR UPDATE`` on PostgreSQL) a compare-and-swap writer holds until
    its transaction ends: concurrent writers queue on the row and each then reads the version the previous one
    committed, so two of them can never pass the same ``expected_version``."""
    query = (
        select(Decision)
        .where(
            func.lower(func.trim(Decision.title)) == title.strip().casefold(),
            scope_equals(Decision.repo_path, decision_scope(repo_path)),
            Decision.superseded_by_id.is_(None),
        )
        .order_by(Decision.id.desc())
        .limit(1)
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    result = await session.execute(query)
    return result.scalar_one_or_none()


async def _current_version(session: AsyncSession, title: str, repo_path: Optional[str]) -> int:
    current = await find_canonical_decision(session, title, repo_path)
    return (current.version or 1) if current is not None else 0


async def _record_decision_in(session: AsyncSession, normalized_title: str, normalized_repo_path: str,
                              requested: dict, author_principal_id: Optional[int],
                              expected_version: Optional[int]) -> Tuple[MemoryWriteResult, Decision]:
    """The body of ``DecisionStore.record_decision`` inside the caller's session (flushes, never commits).

    Returns the result and the canonical row it touched."""
    existing = await find_canonical_decision(session, normalized_title, normalized_repo_path, lock=True)
    check_expected_version("decision", normalized_title, expected_version,
                           existing.version if existing is not None else None)
    if existing is not None:
        changes = {
            field: value for field, value in requested.items()
            if value is not None and getattr(existing, field) != value
        }
        if existing.title != normalized_title:
            changes["title"] = normalized_title
        # Authorship stays as recorded: legacy rows keep NULL (no adoption on edit).
        if not changes:
            await session.flush()
            return MemoryWriteResult(id=existing.id, version=existing.version or 1,
                                     created=False, changed=False), existing
        read_version = existing.version
        # Compare-and-swap on the version this writer read, before anything is written: a writer that slipped
        # past the row lock (a database without one) claims nothing and gets the conflict instead of
        # overwriting the winner's version.
        claimed = await session.execute(
            update(Decision)
            .where(Decision.id == existing.id, Decision.superseded_by_id.is_(None),
                   Decision.version.is_(None) if read_version is None else Decision.version == read_version)
            .values(version=(read_version or 1) + 1)
            .execution_options(synchronize_session=False)
        )
        if cast(CursorResult[Any], claimed).rowcount != 1:
            raise MemoryVersionConflict("decision", normalized_title, read_version or 1,
                                        await _current_version(session, normalized_title, normalized_repo_path))
        history = Decision(
            title=existing.title,
            repo_path=existing.repo_path,
            status="superseded",
            superseded_by_id=existing.id,
            author_principal_id=existing.author_principal_id,
            updated_by_principal_id=existing.updated_by_principal_id,
            approved_by_principal_id=existing.approved_by_principal_id,
            supersedes_id=existing.supersedes_id,
            version=read_version or 1,
            created_at=existing.created_at,
            **{field: getattr(existing, field) for field in _DECISION_CONTENT_FIELDS if field != "status"},
        )
        session.add(history)
        await session.flush()
        for field, value in changes.items():
            setattr(existing, field, value)
        existing.version = (read_version or 1) + 1
        existing.supersedes_id = history.id
        existing.updated_by_principal_id = author_principal_id
        # A new version is unreviewed until an approval stamps it (brain/memory/proposals.py).
        existing.approved_by_principal_id = None
        await session.flush()
        return MemoryWriteResult(id=existing.id, version=existing.version, created=False,
                                 changed=True, superseded_id=history.id), existing

    new_decision = Decision(
        title=normalized_title,
        repo_path=normalized_repo_path,
        description=requested["description"],
        status=requested["status"],
        date=requested["date"] or datetime.date.today(),
        reason=requested["reason"],
        consequences=requested["consequences"],
        affected_features=requested["affected_features"],
        affected_modules=requested["affected_modules"],
        affected_files=requested["affected_files"],
        author_principal_id=author_principal_id,
        updated_by_principal_id=author_principal_id,
        version=1,
    )
    try:
        # A savepoint keeps the caller's transaction usable when a concurrent writer created it first.
        async with session.begin_nested():
            session.add(new_decision)
            await session.flush()
    except IntegrityError:
        # uq_decisions_normalized_title: the same title and scope was created concurrently.
        actual = await _current_version(session, normalized_title, normalized_repo_path)
        if not actual:
            raise
        raise MemoryVersionConflict("decision", normalized_title, int(expected_version or 0), actual) from None
    return MemoryWriteResult(id=new_decision.id, version=1, created=True, changed=True), new_decision


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
        session: Optional[AsyncSession] = None,
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
        kept from the first record; every later change names its principal.

        With ``session`` the write is only flushed, so the caller's transaction
        decides whether it commits (proposal approval writes the decision and
        the proposal's state change atomically)."""
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

        async def _write(sess: AsyncSession) -> Tuple[MemoryWriteResult, Decision]:
            return await _record_decision_in(sess, normalized_title, normalized_repo_path, requested,
                                             author_principal_id, expected_version)

        if session is not None:
            return (await _write(session))[0]
        async with async_session_factory() as own_session:
            written, row = await _write(own_session)
            await own_session.commit()
            if not written.changed:
                return written
            # Committed state is authoritative for the id and version the caller gets back.
            await own_session.refresh(row)
            return dataclasses.replace(written, id=row.id, version=row.version or written.version)

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
