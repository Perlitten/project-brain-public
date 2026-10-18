"""Read-only team diagnostics: ``brain team doctor`` and ``GET /team/doctor``.

Every check reads settings, the database or the backup directory and reports
``pass``, ``warn``, ``fail`` or ``unknown`` with a one-line fix hint. Nothing
here writes: fixes are the operator's, through the commands the hints name.
``unknown`` means the Brain holds no evidence either way (for example no
backup location is configured); it is never a guess.
"""
from __future__ import annotations

import json
import os
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.config.settings import settings
from brain.database.models import ApiCredential, Principal, Repository, RepositoryOriginState
from brain.memory.source_manifest import revision_matches
from brain.auth.principals import ALL_SCOPES
from brain.team.authorization import team_state
from brain.team.execution import operator_policy
from brain.team.models import (AgentRun, AgentRunExecution, TeamAccessFence, TeamIdentity, TeamInvitation,
                               TeamMember)
from brain.team.provisioning import TEAM_ID
from brain.team.restore_fence import fence_table_exists

Status = Literal["pass", "warn", "fail", "unknown"]
STATUSES: tuple[Status, ...] = ("pass", "warn", "fail", "unknown")
# A requested execution no worker has claimed for this long probably has no executor to run it.
UNCLAIMED_AFTER = timedelta(hours=1)
# Origin state older than this many poll intervals (and at least an hour) is stale.
STALE_POLLS = 3


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    message: str
    hint: str = ""

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _names(values: list[str], limit: int = 5) -> str:
    shown = ", ".join(values[:limit])
    return shown + (f" (+{len(values) - limit} more)" if len(values) > limit else "")


def _live_credential(now: datetime):
    return and_(ApiCredential.revoked_at.is_(None),
                or_(ApiCredential.expires_at.is_(None), ApiCredential.expires_at > now))


def _live_member():
    return and_(TeamMember.team_id == TEAM_ID, TeamMember.verified_at.is_not(None), TeamMember.revoked_at.is_(None))


# --- settings-only checks -------------------------------------------------------------------------------------


def check_oidc() -> Check:
    enabled = bool(settings.BRAIN_OIDC_ENABLED)
    issuer = (settings.BRAIN_OIDC_ISSUER or "").strip()
    client_id = (settings.BRAIN_OIDC_CLIENT_ID or "").strip()
    if not enabled:
        if settings.BRAIN_OIDC_REQUIRE_FOR_INVITATIONS:
            return Check("oidc", "warn", "BRAIN_OIDC_REQUIRE_FOR_INVITATIONS is set but SSO is disabled",
                         "Set BRAIN_OIDC_ENABLED=true or unset BRAIN_OIDC_REQUIRE_FOR_INVITATIONS.")
        if issuer or client_id:
            return Check("oidc", "warn", "BRAIN_OIDC_ISSUER/CLIENT_ID are set but BRAIN_OIDC_ENABLED is false",
                         "Set BRAIN_OIDC_ENABLED=true to use SSO, or remove the unused BRAIN_OIDC_* values.")
        return Check("oidc", "pass", "SSO is disabled; members sign in with named keys")
    missing = [name for name, value in (("BRAIN_OIDC_ISSUER", issuer), ("BRAIN_OIDC_CLIENT_ID", client_id))
               if not value]
    if missing:
        return Check("oidc", "fail", f"SSO is enabled without {', '.join(missing)}",
                     "Set the missing BRAIN_OIDC_* values (docs/operations/TEAM_SSO.md).")
    redirect = (settings.BRAIN_OIDC_REDIRECT_URI or "").strip()
    web = (settings.BRAIN_WEB_URL or "").strip()
    if redirect and web and urlsplit(redirect).netloc.lower() != urlsplit(web).netloc.lower():
        return Check("oidc", "warn", f"BRAIN_OIDC_REDIRECT_URI host differs from BRAIN_WEB_URL ({web})",
                     "Point BRAIN_OIDC_REDIRECT_URI at <BRAIN_WEB_URL>/auth/oidc/callback.")
    return Check("oidc", "pass", f"SSO is enabled with issuer {issuer}")


def check_execution_config() -> Check:
    policy = operator_policy()
    if policy.error:
        return Check("execution_config", "fail", policy.error,
                     "Fix the JSON in BRAIN_EXECUTION_TOOLS / BRAIN_EXECUTION_SECRETS (docs/operations/TEAM_EXECUTION.md).")
    if not policy.enabled:
        return Check("execution_config", "pass", "Execution is disabled on this process")
    if policy.root is None:
        return Check("execution_config", "fail", "Execution is enabled but BRAIN_EXECUTION_ROOT is not set",
                     "Set BRAIN_EXECUTION_ROOT to a writable directory owned by the worker.")
    problems: list[str] = []
    if not policy.root.is_dir():
        problems.append(f"BRAIN_EXECUTION_ROOT {policy.root} does not exist")
    for name, spec in sorted(policy.tools.items()):
        program = spec.argv[0]
        if not (shutil.which(program) or (os.path.isabs(program) and os.access(program, os.X_OK))):
            problems.append(f"tool {name!r} runs {program!r}, which is not found")
    for ref, variable in sorted(policy.secrets.items()):
        if not os.environ.get(variable):
            problems.append(f"secret {ref!r} reads ${variable}, which is not set")
    if problems:
        return Check("execution_config", "fail", "; ".join(problems),
                     "Create the root, install the tools or set the referenced environment variables on the worker.")
    if policy.egress:
        return Check("execution_config", "warn",
                     f"BRAIN_EXECUTION_EGRESS lists {len(policy.egress)} host(s); the subprocess backend refuses egress",
                     "Unset BRAIN_EXECUTION_EGRESS unless a backend that enforces egress is configured.")
    if not policy.tools:
        return Check("execution_config", "warn", "Execution is enabled but BRAIN_EXECUTION_TOOLS lists no tools",
                     "Declare the allowed tools in BRAIN_EXECUTION_TOOLS.")
    return Check("execution_config", "pass",
                 f"Execution is enabled: {len(policy.tools)} tool(s), {len(policy.secrets)} secret ref(s), "
                 f"root {policy.root}")


def check_origin_settings() -> Check:
    if settings.BRAIN_ORIGIN_POLL_INTERVAL_S <= 0:
        return Check("origin_settings", "warn", "Origin polling is disabled (BRAIN_ORIGIN_POLL_INTERVAL_S=0)",
                     "Set BRAIN_ORIGIN_POLL_INTERVAL_S (default 300) so indexed sources follow origin.")
    raw = (settings.BRAIN_ORIGIN_REMOTES or "").strip()
    if not raw:
        return Check("origin_settings", "pass",
                     "BRAIN_ORIGIN_REMOTES is not set; repositories are polled through their own 'origin' remote")
    try:
        data = json.loads(raw)
    except ValueError:
        return Check("origin_settings", "fail", "BRAIN_ORIGIN_REMOTES is not valid JSON and is ignored",
                     'Use {"/path": "https://host/org/repo.git"} or {"/path": {"url": "...", "branch": "main"}}.')
    if not isinstance(data, dict):
        return Check("origin_settings", "fail", "BRAIN_ORIGIN_REMOTES must be a JSON object and is ignored",
                     'Use {"/path": "https://host/org/repo.git"}.')
    skipped = [str(path) for path, value in data.items()
               if not ((isinstance(value, str) and value.strip())
                       or (isinstance(value, dict) and isinstance(value.get("url"), str) and value["url"].strip()))]
    if skipped:
        return Check("origin_settings", "warn", f"BRAIN_ORIGIN_REMOTES entries without a URL are skipped: "
                     f"{_names(skipped)}", "Give each entry a URL string or an object with a 'url' key.")
    return Check("origin_settings", "pass", f"BRAIN_ORIGIN_REMOTES names {len(data)} source(s)")


def _manifest_evidence(path: Path) -> tuple[datetime, str, str] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        stamp = datetime.fromisoformat(str(data["timestamp"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return _aware(stamp) or stamp, str(data.get("status") or "unknown"), str(path)


MIN_DUMP_BYTES = 1024


def latest_backup(directory: Path) -> tuple[datetime, str, str] | None:
    """Newest backup evidence under ``directory`` (one level deep): (time, status, path).

    ``backup_manifest.json`` (scripts/backup_runner.py) carries its own time and
    status; a bare ``brain_db.sql.gz`` (deploy/server_up.sh) or
    ``brain_backup_*`` dump counts by modification time, with status ``empty``
    when it is too small to hold a dump."""
    found: list[tuple[datetime, str, str]] = []
    for pattern in ("backup_manifest.json", "*/backup_manifest.json"):
        for path in directory.glob(pattern):
            evidence = _manifest_evidence(path)
            if evidence is not None:
                found.append(evidence)
    for pattern in ("brain_db.sql.gz", "*/brain_db.sql.gz", "brain_backup_*"):
        for path in directory.glob(pattern):
            try:
                if not path.is_file():
                    continue
                info = path.stat()
            except OSError:
                continue
            # server_up.sh pipes pg_dump into gzip, so a failed dump still leaves
            # a few bytes of gzip framing behind; no real dump is that small.
            status = "ok" if info.st_size >= MIN_DUMP_BYTES else "empty"
            found.append((datetime.fromtimestamp(info.st_mtime, timezone.utc), status, str(path)))
    return max(found, key=lambda item: item[0]) if found else None


def check_backup(now: datetime | None = None) -> Check:
    configured = (settings.BRAIN_BACKUP_DIR or "").strip()
    if not configured:
        return Check("backup", "unknown", "No backup location is configured, so backup status is unknown",
                     "Set BRAIN_BACKUP_DIR to the directory backups land in (mounted read-only is enough).")
    directory = Path(configured)
    if not directory.is_dir():
        return Check("backup", "unknown", f"BRAIN_BACKUP_DIR {directory} is not readable from this process",
                     "Mount the backup directory into this container or fix BRAIN_BACKUP_DIR.")
    try:
        evidence = latest_backup(directory)
    except OSError as exc:
        return Check("backup", "unknown", f"Could not read {directory}: {type(exc).__name__}",
                     "Make the backup directory readable by the Brain user (uid 999 in the image).")
    if evidence is None:
        return Check("backup", "warn", f"No backup found in {directory}",
                     "Run a backup (docs/operations/TEAM_DEPLOY.md, Backup) and schedule it.")
    stamp, status, path = evidence
    if status != "ok":
        return Check("backup", "fail", f"The newest backup ({stamp.isoformat()}) has status {status!r}: {path}",
                     "Read the backup log, fix the cause and run the backup again.")
    age = (now or _now()) - stamp
    hours = int(age.total_seconds() // 3600)
    if age > timedelta(hours=max(1, settings.BRAIN_BACKUP_MAX_AGE_HOURS)):
        return Check("backup", "warn", f"The newest backup is {hours}h old ({stamp.isoformat()}): {path}",
                     "Check the backup schedule (cron or scheduled task) and run a backup now.")
    return Check("backup", "pass", f"The newest backup is {hours}h old ({stamp.isoformat()}): {path}")


# --- database checks ------------------------------------------------------------------------------------------


async def check_team_state(session: AsyncSession) -> Check:
    state = await team_state(session)
    if state == "solo":
        return Check("team_state", "pass", "solo: a team of one, every authenticated key administers this Brain")
    members = (await session.execute(select(func.count()).select_from(TeamMember).where(_live_member()))).scalar_one()
    return Check("team_state", "pass", f"team: {members} live member(s)")


async def check_administrators(session: AsyncSession, state: str, now: datetime) -> Check:
    if state == "solo":
        return Check("administrators", "pass", "Not needed in solo state")
    rows = (await session.execute(
        select(Principal.name, ApiCredential.scopes).join(TeamMember, TeamMember.principal_id == Principal.id)
        .join(ApiCredential, ApiCredential.principal_id == Principal.id)
        .where(_live_member(), TeamMember.is_admin.is_(True), Principal.disabled_at.is_(None),
               _live_credential(now)))).all()
    # team_administrator needs the team:admin scope on the credential, not only the is_admin flag.
    admins = sorted({name for name, scopes in rows if {ALL_SCOPES, "team:admin"} & set(scopes or [])})
    if not admins:
        return Check("administrators", "fail", "No live team administrator holds a live key with team:admin",
                     "Run `brain team member add --principal <name> --admin --mint-key` on the server.")
    return Check("administrators", "pass", f"{len(admins)} administrator(s): {_names(admins)}")


async def check_shared_key(state: str) -> Check:
    """The shared key is refused by ``require_principal`` in team state; report which applies.

    Nothing to fix when it is still set: the dashboard keeps using its copy only to
    seal member cookies, and the API refuses it as a credential."""
    shared = bool((settings.PROJECT_BRAIN_API_KEY or "").strip())
    if state == "solo":
        if shared:
            return Check("shared_key", "pass", "Solo state: the shared server key is accepted")
        return Check("shared_key", "pass", "Solo state: no shared server key is configured")
    if shared:
        return Check("shared_key", "pass",
                     "PROJECT_BRAIN_API_KEY is refused as a credential while the Brain has team members")
    return Check("shared_key", "pass", "No shared server key is configured; members use named keys")


async def check_member_credentials(session: AsyncSession, state: str, now: datetime) -> Check:
    if state == "solo":
        return Check("member_credentials", "pass", "Not needed in solo state")
    has_key = exists().where(ApiCredential.principal_id == TeamMember.principal_id, _live_credential(now))
    conditions = [~has_key]
    if settings.BRAIN_OIDC_ENABLED:
        conditions.append(~exists().where(TeamIdentity.principal_id == TeamMember.principal_id))
    names = (await session.execute(
        select(Principal.name).join(TeamMember, TeamMember.principal_id == Principal.id)
        .where(_live_member(), *conditions).order_by(Principal.name))).scalars().all()
    if names:
        how = "no live key and no SSO identity" if settings.BRAIN_OIDC_ENABLED else "no live key"
        return Check("member_credentials", "warn", f"{len(names)} live member(s) with {how}: {_names(list(names))}",
                     "Mint a key with `brain team member add --principal <name> --mint-key` "
                     "or bind SSO with `brain team member sso-bind`.")
    return Check("member_credentials", "pass", "Every live member can sign in")


async def check_revocations(session: AsyncSession, now: datetime) -> Check:
    revoked = and_(TeamMember.team_id == TEAM_ID, TeamMember.revoked_at.is_not(None))
    with_keys = (await session.execute(
        select(Principal.name).join(TeamMember, TeamMember.principal_id == Principal.id)
        .where(revoked, exists().where(ApiCredential.principal_id == Principal.id, _live_credential(now)))
        .order_by(Principal.name))).scalars().all()
    with_runs = (await session.execute(
        select(Principal.name).join(TeamMember, TeamMember.principal_id == Principal.id)
        .where(revoked, exists().where(AgentRun.initiator_id == Principal.id, AgentRun.revoked_at.is_(None)))
        .order_by(Principal.name))).scalars().all()
    pending = sorted(set(with_keys) | set(with_runs))
    if pending:
        return Check("revocations", "fail",
                     f"Revoked member(s) still hold live credentials or runs: {_names(pending)}",
                     "Run `brain team member revoke --principal <name>` again; it revokes what is left.")
    return Check("revocations", "pass", "No access survives a revoked membership")


async def check_restore_fence(session: AsyncSession) -> Check:
    if not await fence_table_exists(session):
        return Check("restore_fence", "pass", "No restore fence (this Brain was never restored from a team snapshot)")
    fences = (await session.execute(select(TeamAccessFence).where(TeamAccessFence.cleared_at.is_(None)))).scalars().all()
    if fences:
        return Check("restore_fence", "fail", f"Team access is fenced after a restore ({len(fences)} open fence(s))",
                     "Run `brain team reconcile`, correct the state, then `brain team reconcile --attest <digest>`.")
    return Check("restore_fence", "pass", "No open restore fence")


async def check_invitations(session: AsyncSession, now: datetime) -> Check:
    pending = and_(TeamInvitation.team_id == TEAM_ID, TeamInvitation.accepted_at.is_(None),
                   TeamInvitation.revoked_at.is_(None))
    stale = (await session.execute(select(TeamInvitation.id).where(pending, TeamInvitation.expires_at <= now)
                                   .order_by(TeamInvitation.expires_at))).scalars().all()
    if stale:
        return Check("invitations", "warn", f"{len(stale)} invitation(s) expired unaccepted: "
                     f"{_names([str(item) for item in stale], 3)}",
                     "Revoke them with `brain team invite revoke <id>` (and re-invite if still wanted).")
    open_count = (await session.execute(select(func.count()).select_from(TeamInvitation).where(pending))).scalar_one()
    return Check("invitations", "pass", f"{open_count} pending invitation(s), none expired")


async def check_execution_leases(session: AsyncSession, now: datetime) -> Check:
    orphaned = (await session.execute(select(AgentRunExecution.run_id).where(
        AgentRunExecution.released_at.is_(None), AgentRunExecution.started_at.is_not(None),
        AgentRunExecution.lease_expires_at <= now))).scalars().all()
    if orphaned:
        return Check("execution_leases", "fail",
                     f"{len(orphaned)} execution(s) hold an expired lease: {_names([str(r) for r in orphaned], 3)}",
                     "Run `brain team execution recover` (a restarted worker also does this).")
    unclaimed = (await session.execute(
        select(AgentRunExecution.run_id).join(AgentRun, AgentRun.id == AgentRunExecution.run_id).where(
            AgentRunExecution.released_at.is_(None), AgentRunExecution.started_at.is_(None),
            AgentRunExecution.cancel_requested_at.is_(None), AgentRun.revoked_at.is_(None),
            AgentRunExecution.created_at <= now - UNCLAIMED_AFTER))).scalars().all()
    if unclaimed:
        return Check("execution_leases", "warn",
                     f"{len(unclaimed)} execution(s) requested over an hour ago and never started: "
                     f"{_names([str(r) for r in unclaimed], 3)}",
                     "Start a worker with BRAIN_EXECUTION_ENABLED=true (compose profile `executor`) or cancel them.")
    return Check("execution_leases", "pass", "No stuck execution leases")


async def check_sources(session: AsyncSession, now: datetime) -> list[Check]:
    rows = (await session.execute(
        select(Repository, RepositoryOriginState)
        .outerjoin(RepositoryOriginState, RepositoryOriginState.repository_id == Repository.id)
        .order_by(Repository.id))).all()
    if not rows:
        return [Check("source_freshness", "pass", "No indexed repositories")]
    interval = max(0, settings.BRAIN_ORIGIN_POLL_INTERVAL_S)
    stale_after = max(timedelta(hours=1), timedelta(seconds=interval * STALE_POLLS))
    checks: list[Check] = []
    for repository, origin in rows:
        name = f"source_freshness:{repository.name}"
        if origin is None:
            checks.append(Check(name, "unknown", "Origin has not been polled for this repository",
                                "Wait for the origin poll or check that the scheduler runs "
                                "(BRAIN_ORIGIN_POLL_INTERVAL_S > 0)."))
            continue
        if origin.poll_status == "unconfigured":
            checks.append(Check(name, "warn", "No origin to poll for this repository",
                                f"Add {repository.path} to BRAIN_ORIGIN_REMOTES or give the checkout an 'origin' remote."))
            continue
        if origin.poll_status == "error":
            checks.append(Check(name, "warn", f"The last origin poll failed: {origin.poll_error or 'no detail'}",
                                "Check the remote URL, credentials and network for BRAIN_ORIGIN_REMOTES."))
            continue
        polled = _aware(origin.polled_at)
        if polled is None or now - polled > stale_after:
            when = polled.isoformat() if polled else "never"
            checks.append(Check(name, "warn", f"Origin state is stale (last poll {when})",
                                "Check that the scheduler and worker run and BRAIN_ORIGIN_POLL_INTERVAL_S > 0."))
            continue
        indexed = repository.last_indexed_commit
        remote = origin.remote_head
        if remote and not revision_matches(remote, indexed):
            behind = f", {origin.commits_behind} commit(s) behind" if origin.commits_behind else ""
            checks.append(Check(name, "warn", f"Indexed {(indexed or 'nothing')[:12]}, origin "
                                f"{origin.tracked_branch or 'HEAD'} is at {remote[:12]}{behind}",
                                "The poll queues a reindex when origin moves: check the worker, or run "
                                "`brain index` for this repository."))
            continue
        checks.append(Check(name, "pass", f"Indexed revision matches origin ({(remote or indexed or '?')[:12]})"))
    return checks


Probe = Callable[[], Awaitable[Check | list[Check]]]


async def _guarded(session: AsyncSession, name: str, probe: Probe) -> list[Check]:
    """Run one database probe in a savepoint so a missing table reports ``unknown`` and spares the rest."""
    try:
        async with session.begin_nested():
            result = await probe()
    except Exception as exc:  # a diagnostic must report, not crash, on a schema or connection problem
        return [Check(name, "unknown", f"Could not read: {type(exc).__name__}",
                      "Run database migrations (`alembic upgrade head`) and check the database connection.")]
    return result if isinstance(result, list) else [result]


async def run_checks(session: AsyncSession, now: datetime | None = None) -> list[Check]:
    """Every team diagnostic, in a fixed order. Read-only."""
    now = now or _now()
    checks: list[Check] = []
    full = settings.BRAIN_STORAGE_MODE == "full"
    if not full:
        checks.append(Check("team_state", "pass", "solo: Lite storage is always a team of one"))
        state = "solo"
    else:
        checks += await _guarded(session, "team_state", lambda: check_team_state(session))
        try:
            state = await team_state(session)
        except Exception:
            state = "unknown"
    checks.append(check_oidc())
    checks.append(await check_shared_key(state))
    if full:
        probes: list[tuple[str, Probe]] = [
            ("administrators", lambda: check_administrators(session, state, now)),
            ("member_credentials", lambda: check_member_credentials(session, state, now)),
            ("revocations", lambda: check_revocations(session, now)),
            ("restore_fence", lambda: check_restore_fence(session)),
            ("invitations", lambda: check_invitations(session, now)),
            ("execution_leases", lambda: check_execution_leases(session, now)),
        ]
        for name, probe in probes:
            checks += await _guarded(session, name, probe)
    checks.append(check_execution_config())
    checks.append(check_origin_settings())
    if full:
        checks += await _guarded(session, "source_freshness", lambda: check_sources(session, now))
    checks.append(check_backup(now))
    return checks


def summary(checks: list[Check]) -> dict[str, Any]:
    counts = {status: sum(1 for check in checks if check.status == status) for status in STATUSES}
    overall: Status = next((status for status in ("fail", "warn", "unknown") if counts[status]), "pass")
    return {"status": overall, "counts": counts, "checks": [check.as_dict() for check in checks]}
