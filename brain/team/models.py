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
    # Set only by ``brain team dissolve``: a team whose members are all revoked
    # stays a team (the shared key and member keys get no implicit authority)
    # until an operator dissolves it explicitly.
    dissolved_at: Mapped[datetime | None] = mapped_column(utc_datetime(), nullable=True)


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
    # Set when the invitation was accepted through OIDC single sign-on: the
    # identity provider vouched for ``email`` and these identify the account.
    verified_issuer: Mapped[str | None] = mapped_column(String(255), nullable=True)
    verified_subject: Mapped[str | None] = mapped_column(String(255), nullable=True)


class TeamIdentity(Base):
    """An external (OIDC) identity bound to a team member: who may sign in with SSO as that member.

    Written when an invitation is accepted through SSO, or by an operator
    (``brain team member sso-bind``) for a member who joined another way. One
    identity maps to exactly one member; there is no auto-provisioning, so an
    identity without a row here cannot sign in."""
    __tablename__ = "team_identities"
    __table_args__ = (UniqueConstraint("issuer", "subject", name="uq_team_identities_issuer_subject"),
                      UniqueConstraint("principal_id", "issuer", name="uq_team_identities_principal_issuer"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), nullable=False)
    principal_id: Mapped[int] = mapped_column(ForeignKey("principals.id"), nullable=False, index=True)
    issuer: Mapped[str] = mapped_column(String(255), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    invitation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("team_invitations.id"), nullable=True)
    bound_by: Mapped[str] = mapped_column(String(160), nullable=False)
    created_at: Mapped[datetime] = mapped_column(utc_datetime(), server_default=func.now(), nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(utc_datetime(), nullable=True)
    # The dashboard credential the latest SSO sign-in (or SSO acceptance) minted. The next sign-in revokes
    # it, so a member holds at most one live SSO credential per identity; logout and unbinding revoke it.
    # No foreign key: it is a pointer to revoke, and a dangling id revokes nothing.
    session_credential_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class TeamOidcFlow(Base):
    """One pending OIDC authorization request (single use, short-lived).

    Only hashes of the ``state`` value and of the browser binding secret are
    stored; the nonce and the PKCE verifier stay server-side and are spent with
    the row."""
    __tablename__ = "team_oidc_flows"
    __table_args__ = (CheckConstraint("purpose IN ('signin','invitation')", name="ck_team_oidc_flow_purpose"),)
    state_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    binding_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    nonce: Mapped[str] = mapped_column(String(128), nullable=False)
    code_verifier: Mapped[str] = mapped_column(String(128), nullable=False)
    purpose: Mapped[str] = mapped_column(String(16), nullable=False)
    invitation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("team_invitations.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(utc_datetime(), server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(utc_datetime(), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(utc_datetime(), nullable=True)


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


class TeamExecutionGrant(Base):
    """A separate, explicit permission for one member to run bounded executors in one project (M2).

    Not implied by any project role or by team administration: a team
    administrator records it per member and project, and it only takes effect
    while the operator has enabled execution (``BRAIN_EXECUTION_ENABLED``) and
    the member still holds a Contributor or Reviewer role there."""
    __tablename__ = "team_execution_grants"
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("team_projects.id"), primary_key=True)
    principal_id: Mapped[int] = mapped_column(ForeignKey("principals.id"), primary_key=True)
    granted_by: Mapped[str] = mapped_column(String(128), nullable=False)
    granted_at: Mapped[datetime] = mapped_column(utc_datetime(), nullable=False, server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(utc_datetime(), nullable=True)
    revoked_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class AgentRunExecution(Base):
    """Capabilities, plan and lease of the one bounded execution a run may have (M2).

    Capabilities are data: the tool allowlist, secret *references* (names the
    server resolves; never values), the egress allowlist and the budget. The
    outcome is not a column: it is derived from the ``run.execution.*`` events
    in ``harness.agent_task_events``, so the ledger stays the one status
    record. ``fencing_token`` increases on every lease takeover; a writer must
    present the current token, its own owner id and an unexpired lease."""
    __tablename__ = "agent_run_executions"
    __table_args__ = {"schema": HARNESS_SCHEMA}
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{HARNESS_SCHEMA}.agent_runs.id"), primary_key=True)
    request_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_by: Mapped[int] = mapped_column(ForeignKey("principals.id"), nullable=False)
    requested_credential_id: Mapped[int] = mapped_column(ForeignKey("api_credentials.id"), nullable=False)
    steps: Mapped[list] = mapped_column(JSON, nullable=False)
    tools: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    secret_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    egress: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    budget: Mapped[dict] = mapped_column(JSON, nullable=False)
    backend: Mapped[str] = mapped_column(String(32), nullable=False)
    # Operator execution environment (container backend); NULL for the subprocess backend.
    environment: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    fencing_token: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_expires_at: Mapped[datetime | None] = mapped_column(utc_datetime(), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(utc_datetime(), nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(utc_datetime(), nullable=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(utc_datetime(), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(utc_datetime(), nullable=True)
    steps_done: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(utc_datetime(), nullable=False, server_default=func.now())


class TeamActivityEvent(Base):
    """Append-only record of team activity that leaves no other timestamped trace.

    Membership and project-role changes only flip ``revoked_at``/``role`` in
    place, and queued jobs live in Redis, so neither can be read back as a
    dated history. Provisioning and the job endpoints write one row here per
    effective change. Memory versions, invitations, team tasks and runs are
    derived from their own tables by ``brain.team.activity`` instead.

    ``audience`` decides who may read the row: ``admin`` for team
    administration (memberships), ``repository`` for rows bound to
    ``repository_id`` (readable by members with ``read`` on it)."""
    __tablename__ = "team_activity_events"
    __table_args__ = (CheckConstraint("audience IN ('admin','repository')", name="ck_team_activity_audience"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(utc_datetime(), server_default=func.now(), nullable=False,
                                                  index=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    audience: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_principal_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    actor_label: Mapped[str | None] = mapped_column(String(160), nullable=True)
    subject_principal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    repository_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    summary: Mapped[str] = mapped_column(String(255), nullable=False)
    dedupe_key: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    details: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class TeamTaskPlan(Base):
    """Coordination data of one team task (M3): its parent, sibling dependencies and declared scope.

    A subtask is an ordinary team task (``team_task_bindings``) with a plan row
    naming its parent. ``depends_on`` lists sibling task ids (same parent) that
    must be done before a run for this task may be requested; the dependency
    graph is kept acyclic on every write. ``scope_paths`` are the
    repository-relative paths or globs the task declares it will touch; overlap
    detection compares them with other active tasks and live leases."""
    __tablename__ = "team_task_plans"
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("team_task_bindings.task_id"), primary_key=True)
    parent_task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("team_task_bindings.task_id"), nullable=True,
                                                             index=True)
    depends_on: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    scope_paths: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    updated_by: Mapped[int] = mapped_column(ForeignKey("principals.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(utc_datetime(), nullable=False, server_default=func.now())


class TeamResourceLease(Base):
    """An exclusive, expiring lease on a repository path or named resource (M3 critical leases).

    One row per (repository, resource); acquiring a released or expired lease
    is a compare-and-swap on that row that increases ``fencing_token``, the same
    pattern as the M2 execution lease (``agent_run_executions``). Renewing or
    releasing must present the holder and the current token, so a holder whose
    lease expired or was taken over can change nothing. Revoking a member
    releases the leases held by the member and by the runs they initiated."""
    __tablename__ = "team_resource_leases"
    __table_args__ = (UniqueConstraint("repository_id", "resource", name="uq_team_resource_leases_resource"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("team_projects.id"), nullable=False, index=True)
    repository_id: Mapped[int] = mapped_column(ForeignKey("team_project_repositories.repository_id"), nullable=False)
    resource: Mapped[str] = mapped_column(String(512), nullable=False)
    holder_id: Mapped[int] = mapped_column(ForeignKey("principals.id"), nullable=False, index=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("team_task_bindings.task_id"), nullable=True)
    fencing_token: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    acquired_at: Mapped[datetime] = mapped_column(utc_datetime(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(utc_datetime(), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(utc_datetime(), nullable=True)
    released_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)


class TeamRunResolution(Base):
    """The rule and decision versions a run resolved when it was requested (M3 instruction resolution).

    ``resolved`` is a list of ``{"kind": "decision"|"rule", "id": ..., "version": n}``
    for the canonical, active rows that applied to the task's repository (and
    the global ones) at that moment. Integration compares it with the current
    canonical rows and flags the run when any of them was edited, superseded or
    retired since."""
    __tablename__ = "team_run_resolutions"
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{HARNESS_SCHEMA}.agent_runs.id"), primary_key=True)
    resolved: Mapped[list[dict]] = mapped_column(JSON, nullable=False, default=list)
    resolved_at: Mapped[datetime] = mapped_column(utc_datetime(), nullable=False, server_default=func.now())


class TeamIntegrationEntry(Base):
    """One completed run waiting for integration, in queue order (``id``) per repository (M3).

    No git merge happens here: an entry records the run's base and head, and
    ``integrate`` re-validates it against the current base revision of the
    target. A different base gives ``needs_rebase``; instructions superseded
    since the run started give ``stale_instructions``; otherwise the entry is
    ``integrated`` by an independent reviewer. ``version`` guards every
    transition (compare-and-swap)."""
    __tablename__ = "team_integration_entries"
    __table_args__ = (CheckConstraint("status IN ('queued','needs_rebase','stale_instructions','integrated','rejected')",
                                      name="ck_team_integration_status"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("team_projects.id"), nullable=False, index=True)
    repository_id: Mapped[int] = mapped_column(ForeignKey("team_project_repositories.repository_id"), nullable=False)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("team_task_bindings.task_id"), nullable=False, index=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{HARNESS_SCHEMA}.agent_runs.id"), nullable=False,
                                              unique=True)
    base_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    head_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    checked_base_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    checked_at: Mapped[datetime | None] = mapped_column(utc_datetime(), nullable=True)
    enqueued_by: Mapped[int] = mapped_column(ForeignKey("principals.id"), nullable=False)
    enqueued_at: Mapped[datetime] = mapped_column(utc_datetime(), nullable=False, server_default=func.now())
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("principals.id"), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(utc_datetime(), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
