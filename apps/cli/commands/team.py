"""Operator provisioning for the experimental team layer.

These commands write directly to the database as a trusted operator action,
so they work before ``BRAIN_TEAM_MODE`` is switched on (the API refuses team
routes until an administrator exists). Every command is idempotent: repeating
it with the same arguments is a no-op, contradictory arguments fail with a
conflict instead of rewriting state.
"""
from __future__ import annotations

import asyncio
import json
from typing import Optional

import typer

from brain.config.settings import settings
from brain.database.session import async_session_factory, init_db
from brain.team import provisioning
from brain.team.authorization import TeamConflict
from brain.team.provisioning import TeamNotFound

team_cli = typer.Typer(help="Provision the experimental team layer (operator-only, idempotent).")
project_cli = typer.Typer(help="Team projects.")
member_cli = typer.Typer(help="Team members and project roles.")
repo_cli = typer.Typer(help="Repositories attached to projects.")
team_cli.add_typer(project_cli, name="project")
team_cli.add_typer(member_cli, name="member")
team_cli.add_typer(repo_cli, name="repo")


def _fail(message: str) -> None:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


def _run(coro):
    if settings.BRAIN_STORAGE_MODE != "full":
        coro.close()
        _fail("Team provisioning requires BRAIN_STORAGE_MODE=full (Postgres).")
    try:
        return asyncio.run(coro)
    except (TeamConflict, TeamNotFound) as exc:
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
