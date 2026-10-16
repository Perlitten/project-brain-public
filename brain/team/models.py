from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from brain.database.harness_models import HARNESS_SCHEMA
from brain.database.models import Base
from brain.database.portable_types import utc_datetime


class Team(Base):
    __tablename__ = "teams"
    __table_args__ = (CheckConstraint("id = 1", name="ck_single_team"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    name: Mapped[str] = mapped_column(String(128), nullable=False)


class TeamAccessFence(Base):
    """A restore fence: while any row has ``cleared_at IS NULL`` no named credential is honoured.

    ``scripts/restore_database.py`` writes one inside the restore transaction
    whenever the snapshot holds team members, because revocations made after
    the snapshot are not in it. ``brain team reconcile`` clears it. The
    restore SQL (``scripts/team_restore_fence.sql``) repeats this DDL for
    dumps taken before the table existed; keep the two in step."""
    __tablename__ = "team_access_fences"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    fenced_at: Mapped[datetime] = mapped_column(utc_datetime(), nullable=False, server_default=func.now())
    cleared_at: Mapped[datetime | None] = mapped_column(utc_datetime(), nullable=True)
    cleared_by: Mapped[str | None] = mapped_column(Text, nullable=True)


class TeamMember(Base):
    __tablename__ = "team_members"
    principal_id: Mapped[int] = mapped_column(ForeignKey("principals.id"), primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), nullable=False)
    verification_issuer: Mapped[str] = mapped_column(String(255), nullable=False)
    verification_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(utc_datetime(), nullable=True)
    is_admin: Mapped[bool] = mapped_column(default=False)
    revoked_at: Mapped[datetime | None] = mapped_column(utc_datetime(), nullable=True)
    __table_args__ = (UniqueConstraint("verification_issuer", "verification_subject"),)


class TeamProject(Base):
    __tablename__ = "team_projects"
    __table_args__ = (UniqueConstraint("team_id", "name", name="uq_team_projects_team_name"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    permissions: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    disabled_at: Mapped[datetime | None] = mapped_column(utc_datetime(), nullable=True)


class ProjectMember(Base):
    __tablename__ = "team_project_members"
    __table_args__ = (CheckConstraint("role IN ('reader','contributor','reviewer')", name="ck_team_role"),)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("team_projects.id"), primary_key=True)
    principal_id: Mapped[int] = mapped_column(ForeignKey("team_members.principal_id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    revoked_at: Mapped[datetime | None] = mapped_column(utc_datetime(), nullable=True)


class ProjectRepository(Base):
    __tablename__ = "team_project_repositories"
    repository_id: Mapped[int] = mapped_column(ForeignKey("repositories.id"), primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("team_projects.id"), nullable=False, index=True)


class TeamTaskBinding(Base):
    __tablename__ = "team_task_bindings"
    __table_args__ = (UniqueConstraint("project_id", "author_id", "request_key"),)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{HARNESS_SCHEMA}.agent_tasks.id"), primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("team_projects.id"), nullable=False, index=True)
    repository_id: Mapped[int] = mapped_column(ForeignKey("team_project_repositories.repository_id"), nullable=False)
    author_id: Mapped[int] = mapped_column(ForeignKey("principals.id"), nullable=False)
    branch: Mapped[str] = mapped_column(String(255), nullable=False)
    base_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    permissions: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    request_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)


class TeamInvitation(Base):
    """A single-use, expiring offer of verified team membership.

    Only the SHA-256 of the token is stored. The lifecycle state (invited,
    expired, verified, active, revoked) is derived from the timestamps and the
    issued member/credential rows rather than kept in a status column."""
    __tablename__ = "team_invitations"
    __table_args__ = (CheckConstraint("role IN ('reader','contributor','reviewer')", name="ck_team_invitation_role"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    principal_name: Mapped[str] = mapped_column(String(128), nullable=False)
    email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    project_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    is_admin: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(utc_datetime(), server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(utc_datetime(), nullable=False)
    invited_by: Mapped[str] = mapped_column(String(160), nullable=False)
    invited_by_principal_id: Mapped[int | None] = mapped_column(ForeignKey("principals.id"), nullable=True)
    accepted_at: Mapped[datetime | None] = mapped_column(utc_datetime(), nullable=True)
    accepted_principal_id: Mapped[int | None] = mapped_column(ForeignKey("principals.id"), nullable=True, unique=True)
    accepted_credential_id: Mapped[int | None] = mapped_column(ForeignKey("api_credentials.id"), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(utc_datetime(), nullable=True)
    revoked_by: Mapped[str | None] = mapped_column(String(160), nullable=True)


class AgentRun(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (UniqueConstraint("task_id", "requested_by", "request_key"), {"schema": HARNESS_SCHEMA})
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("team_task_bindings.task_id"), nullable=False, index=True)
    principal_id: Mapped[int] = mapped_column(ForeignKey("principals.id"), nullable=False, unique=True)
    credential_id: Mapped[int] = mapped_column(ForeignKey("api_credentials.id"), nullable=False, unique=True)
    initiator_id: Mapped[int] = mapped_column(ForeignKey("principals.id"), nullable=False)
    initiator_credential_id: Mapped[int] = mapped_column(ForeignKey("api_credentials.id"), nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey(f"{HARNESS_SCHEMA}.agent_runs.id"), nullable=True)
    permissions: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    executor_capabilities: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    branch: Mapped[str] = mapped_column(String(255), nullable=False)
    base_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    head_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    worktree_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_by: Mapped[int] = mapped_column(ForeignKey("principals.id"), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(utc_datetime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(utc_datetime(), server_default=func.now())
