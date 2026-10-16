"""Operator provisioning for the team layer.

These commands write directly to the database as a trusted operator action:
the first ``member add`` turns a team of one into a team, after which the API
refuses the shared key and applies project roles. Every command is idempotent: repeating
it with the same arguments is a no-op, contradictory arguments fail with a
conflict instead of rewriting state.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from ipaddress import ip_address
from typing import Optional
from urllib.parse import urlsplit

import httpx
import typer

from brain.config.settings import settings
from brain.database.session import async_session_factory, init_db
from brain.team import invitations, provisioning
from brain.team.authorization import TeamConflict
from brain.team.provisioning import TeamNotFound

team_cli = typer.Typer(help="Provision the experimental team layer (operator-only, idempotent).")
project_cli = typer.Typer(help="Team projects.")
member_cli = typer.Typer(help="Team members and project roles.")
repo_cli = typer.Typer(help="Repositories attached to projects.")
invite_cli = typer.Typer(help="Single-use invitations that turn their holder into a verified member.")
team_cli.add_typer(project_cli, name="project")
team_cli.add_typer(member_cli, name="member")
team_cli.add_typer(repo_cli, name="repo")
team_cli.add_typer(invite_cli, name="invite")


def _fail(message: str) -> None:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


def _run(coro):
    if settings.BRAIN_STORAGE_MODE != "full":
        coro.close()
        _fail("Team provisioning requires BRAIN_STORAGE_MODE=full (Postgres).")
    try:
        return asyncio.run(coro)
    except (TeamConflict, TeamNotFound, invitations.InvitationRejected) as exc:
        _fail(str(exc))


def _emit(payload) -> None:
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@team_cli.command(name="init")
def init_team_cmd(name: str = typer.Option(..., "--name", help="Display name of the single team.")):
    """Create the database schema and the team record (safe to repeat)."""

    async def run():
        await init_db()
        async with async_session_factory() as session, session.begin():
            team = await provisioning.init_team(session, name)
            return provisioning.team_dict(team)

    _emit(_run(run()))


@project_cli.command(name="add")
def add_project_cmd(
    name: str = typer.Argument(..., help="Project name (unique within the team)."),
    permission: Optional[list[str]] = typer.Option(
        None, "--permission", help="Permission allowed in this project (repeatable); default: all."),
):
    """Create a project or return the existing one with the same policy."""

    async def run():
        async with async_session_factory() as session, session.begin():
            project = await provisioning.create_project(session, name=name, allowed=permission or None)
            return provisioning.project_dict(project)

    _emit(_run(run()))


@project_cli.command(name="list")
def list_projects_cmd():
    """List team projects."""

    async def run():
        async with async_session_factory() as session:
            return await provisioning.list_projects(session)

    _emit(_run(run()))


@member_cli.command(name="add")
def add_member_cmd(
    principal: str = typer.Option(..., "--principal", help="Principal id or name of a human."),
    issuer: str = typer.Option(provisioning.CREDENTIAL_ISSUER, "--issuer",
                               help="How the operator verified this person (identity provider name)."),
    subject: Optional[str] = typer.Option(None, "--subject", help="Subject at the issuer; default: principal name."),
    admin: bool = typer.Option(False, "--admin", help="Grant team administration."),
    mint_key: bool = typer.Option(False, "--mint-key",
                                  help="Create the human principal if missing and print a new member credential."),
    project: Optional[str] = typer.Option(None, "--project", help="Also grant a role in this project (id or name)."),
    role: str = typer.Option("contributor", "--role", help="Project role: reader, contributor or reviewer."),
):
    """Record verified team membership (optionally minting a credential and a project role)."""

    async def run():
        async with async_session_factory() as session, session.begin():
            raw_key = None
            if mint_key:
                raw_key, identity = await provisioning.create_human_principal(session, principal, is_admin=admin)
            else:
                identity = await provisioning.resolve_principal(session, principal)
            member = await provisioning.add_member(session, principal=identity, issuer=issuer,
                                                   subject=subject or identity.name, is_admin=True if admin else None)
            result = provisioning.member_dict(member, identity)
            if project is not None:
                target = await provisioning.resolve_project(session, project)
                membership = await provisioning.assign_project_member(session, project_id=target.id,
                                                                      principal_id=identity.id, role=role)
                result["project_membership"] = provisioning.project_member_dict(membership)
            return result, raw_key

    result, raw_key = _run(run())
    _emit(result)
    if raw_key:
        typer.secho("New member credential (shown once, never stored in plaintext):", fg=typer.colors.YELLOW, err=True)
        typer.echo(raw_key, err=True)


@member_cli.command(name="revoke")
def revoke_member_cmd(
    principal: str = typer.Option(..., "--principal", help="Principal id or name."),
    project: Optional[str] = typer.Option(None, "--project",
                                          help="Revoke only this project role; default: the whole membership."),
):
    """Revoke team membership (and all project roles) or a single project role."""

    async def run():
        async with async_session_factory() as session, session.begin():
            identity = await provisioning.resolve_principal(session, principal)
            if project is not None:
                target = await provisioning.resolve_project(session, project)
                membership = await provisioning.revoke_project_member(session, project_id=target.id,
                                                                      principal_id=identity.id)
                return provisioning.project_member_dict(membership)
            member = await provisioning.revoke_member(session, principal=identity)
            return provisioning.member_dict(member, identity)

    _emit(_run(run()))


@member_cli.command(name="list")
def list_members_cmd(
    project: Optional[str] = typer.Option(None, "--project", help="List roles in this project instead."),
):
    """List team members, or the roles of one project."""

    async def run():
        async with async_session_factory() as session:
            if project is not None:
                target = await provisioning.resolve_project(session, project)
                return await provisioning.list_project_members(session, target.id)
            return await provisioning.list_members(session)

    _emit(_run(run()))


@repo_cli.command(name="attach")
def attach_repo_cmd(
    project: str = typer.Option(..., "--project", help="Project id or name."),
    repository: str = typer.Option(..., "--repository", help="Indexed repository id or path."),
):
    """Attach an indexed repository to a project (a repository belongs to one project)."""

    async def run():
        async with async_session_factory() as session, session.begin():
            target = await provisioning.resolve_project(session, project)
            repo = await provisioning.resolve_repository(session, repository)
            link = await provisioning.attach_repository(session, project_id=target.id, repository=repo)
            return provisioning.repository_dict(link, repo)

    _emit(_run(run()))


@repo_cli.command(name="list")
def list_repos_cmd(project: str = typer.Option(..., "--project", help="Project id or name.")):
    """List repositories attached to a project."""

    async def run():
        async with async_session_factory() as session:
            target = await provisioning.resolve_project(session, project)
            return await provisioning.list_project_repositories(session, target.id)

    _emit(_run(run()))


CLI_OPERATOR = "operator:cli"


def _show_once(label: str, secret: str) -> None:
    typer.secho(f"{label} (shown once, never stored in plaintext):", fg=typer.colors.YELLOW, err=True)
    typer.echo(secret, err=True)


@invite_cli.command(name="create")
def create_invite_cmd(
    project: Optional[list[str]] = typer.Option(None, "--project", help="Grant a role in this project (repeatable)."),
    role: str = typer.Option("contributor", "--role", help="Project role: reader, contributor or reviewer."),
    admin: bool = typer.Option(False, "--admin", help="The invitee administers the team."),
    email: Optional[str] = typer.Option(None, "--email", help="Recorded for the operator; not verified."),
    name: Optional[str] = typer.Option(None, "--name", help="Principal name of the invitee; default: generated."),
    expires_hours: int = typer.Option(invitations.DEFAULT_TTL_HOURS, "--expires-hours",
                                      help=f"Hours until the token expires (1-{invitations.MAX_TTL_HOURS})."),
):
    """Issue an invitation and print its single-use token once (to stderr)."""

    async def run():
        async with async_session_factory() as session, session.begin():
            return await invitations.create_invitation(
                session, invited_by=CLI_OPERATOR, projects=project or [], role=role, is_admin=admin, email=email,
                name=name, expires_hours=expires_hours)

    token, invitation = _run(run())
    _emit(invitation)
    _show_once("Invitation token", token)


@invite_cli.command(name="list")
def list_invites_cmd():
    """List invitations with their derived state (tokens are never shown)."""

    async def run():
        async with async_session_factory() as session:
            return await invitations.list_invitations(session)

    _emit(_run(run()))


@invite_cli.command(name="revoke")
def revoke_invite_cmd(
    invitation_id: str = typer.Argument(..., help="Invitation id."),
    revoke_member: bool = typer.Option(False, "--revoke-member",
                                       help="Required once accepted: also revoke the member and their credentials."),
):
    """Revoke an invitation (and, for an accepted one, the access it granted)."""
    try:
        target = uuid.UUID(invitation_id)
    except ValueError:
        _fail("Invitation id must be a UUID")

    async def run():
        async with async_session_factory() as session, session.begin():
            return await invitations.revoke_invitation(session, target, revoked_by=CLI_OPERATOR,
                                                       revoke_issued_member=revoke_member)

    _emit(_run(run()))


def _safe_api_url(url: str) -> bool:
    """https anywhere, plain http only to loopback: the token must not cross a network in cleartext."""
    try:
        parsed = urlsplit(url)
        if not parsed.hostname or parsed.username is not None or parsed.password is not None:
            return False
        if parsed.query or parsed.fragment:
            return False
        _ = parsed.port
        if parsed.scheme == "https":
            return True
        return parsed.scheme == "http" and (parsed.hostname == "localhost" or ip_address(parsed.hostname).is_loopback)
    except ValueError:
        return False


@invite_cli.command(name="accept")
def accept_invite_cmd(
    token: Optional[str] = typer.Argument(None, help="Invitation token; prompted for (hidden) when omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", envvar="BRAIN_API_URL",
                                          help="Brain API to redeem against; default: this machine's database."),
):
    """Redeem an invitation and print the new member credential once (to stderr)."""
    secret = token or typer.prompt("Invitation token", hide_input=True)
    if api_url:
        if not _safe_api_url(api_url):
            _fail("--api-url must use https (plain http only to loopback)")
        try:
            response = httpx.post(f"{api_url.rstrip('/')}/team/invitations/accept", json={"token": secret},
                                  timeout=30.0)
        except httpx.HTTPError as exc:
            _fail(f"Could not reach the Brain API: {type(exc).__name__}")
        if response.status_code != 200:
            try:
                detail = response.json().get("detail")
            except ValueError:
                detail = None
            _fail(f"Invitation not accepted (HTTP {response.status_code}): {detail or response.reason_phrase}")
        result = response.json()
    else:

        async def run():
            async with async_session_factory() as session, session.begin():
                return await invitations.accept_invitation(session, secret)

        result = _run(run())
    credential = result.pop("credential", None)
    _emit(result)
    if credential:
        _show_once("New member credential", credential)
    else:
        typer.secho("Invitation was already accepted; the credential was shown only on first acceptance.",
                    fg=typer.colors.YELLOW, err=True)
