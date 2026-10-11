"""First-use readiness: where a fresh single-user install stands on the path
services → repository → provider → index → agent → first pack.

Each step reports ``status`` (``done`` | ``action`` | ``blocked``), a short
``detail`` of what was observed, and a ``hint`` the operator can act on. The
first-use completion criterion is a usable context pack **for the selected
repository** — proof the user got a useful artifact out of this install, not
merely green service LEDs and not a pack produced for some other repository.

That criterion is a *milestone*, not a health check: it is reached once and
stays reached. Re-indexing is routine and makes existing packs describe older
code, so requiring a pack at the current revision made the step un-complete
itself after every commit. Evidence that the milestone was reached is a pack at
the current index revision, a pack built against some earlier completed index
run of this repository, or a recorded milestone from when one was last seen.
Freshness still matters, and is reported in ``detail`` — it just no longer
retracts the step. Switching repositories starts a fresh milestone.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from sqlalchemy import func, select

from brain.config.paths import get_repo_root, resolve_repo_path
from brain.config.settings import settings
from brain.database.models import ContextPack, IndexingRun
from brain.database.repository_utils import get_repository_by_path
from brain.database.session import async_session_factory, check_health
from brain.onboarding.provider_check import provider_state
from brain.onboarding.setup_state import (
    first_pack_milestone,
    last_client_activity,
    load_state,
    record_first_pack,
)

_DONE = "done"
_ACTION = "action"
_BLOCKED = "blocked"


def _provider_status(provider: str, kind: str) -> Dict[str, Any]:
    """One provider slot. ``configured`` proves a key is present;
    ``verified``/``failed`` only come from a recorded live probe whose
    credential fingerprint still matches the current key value."""
    state = provider_state(provider, "llm" if kind == "LLM" else "embedding")
    state["configured"] = state["state"] != "missing"
    return state


def _services_step(health: Dict[str, Any]) -> Dict[str, Any]:
    unhealthy = {
        name: str((svc or {}).get("status") or "unknown")
        for name, svc in health.items()
        if (svc or {}).get("status") != "healthy"
    }
    detail = (
        "postgres, redis and neo4j all healthy"
        if not unhealthy
        else "unhealthy: " + ", ".join(f"{k}={v}" for k, v in sorted(unhealthy.items()))
    )
    return {
        "id": "services",
        "title": "Services running",
        "status": _DONE if not unhealthy else _BLOCKED,
        "detail": detail,
        "hint": "docker compose up -d postgres redis neo4j",
        "services": {name: (svc or {}).get("status") for name, svc in health.items()},
    }


def _repo_step(repo_path: Optional[Path]) -> Dict[str, Any]:
    if repo_path is None:
        return {
            "id": "repo",
            "title": "Choose a repository",
            "status": _BLOCKED,
            "detail": f"TARGET_REPO_PATH ({settings.TARGET_REPO_PATH!r}) is not a directory",
            "hint": "point it at the repository you work in — save it here or edit .env",
        }
    return {
        "id": "repo",
        "title": "Choose a repository",
        "status": _DONE,
        "detail": str(repo_path),
        "hint": "",
    }


def _provider_step() -> Dict[str, Any]:
    llm = _provider_status(settings.DEFAULT_LLM_PROVIDER, "LLM")
    emb = _provider_status(settings.DEFAULT_EMBEDDING_PROVIDER, "embedding")
    ok = all(s["state"] in ("demo", "verified") for s in (llm, emb))
    unverified = any(s["state"] == "configured" for s in (llm, emb))
    hint = ""
    if unverified:
        hint = "run Verify provider credentials below — a key present is not a key verified"
    elif not ok:
        hint = "export the provider key in your environment, then restart"
    return {
        "id": "provider",
        "title": "Provider credentials",
        "status": _DONE if ok else _ACTION,
        "detail": f"llm: {llm['detail']} · embeddings: {emb['detail']}",
        "hint": hint,
        "llm": llm,
        "embedding": emb,
    }


def config_fingerprint() -> str:
    """Identity of the configuration a self-check ran against — a change to
    any of these invalidates the recorded result. Secret values never enter
    the fingerprint; only whether an API key is configured."""
    material = "|".join(
        [
            sys.executable,
            str(get_repo_root()),
            settings.TARGET_REPO_PATH or "",
            settings.DEFAULT_LLM_PROVIDER or "",
            settings.DEFAULT_EMBEDDING_PROVIDER or "",
            "key" if settings.PROJECT_BRAIN_API_KEY else "nokey",
        ]
    )
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def _agent_step() -> Dict[str, Any]:
    """Connect-your-agent evidence, in two layers: the server-side MCP
    self-check (labeled separately, invalidated by config changes) and actual
    external-client activity recorded by the stdio server on initialize.
    'Connected' is only ever claimed from the recorded handshake."""
    state = load_state()
    check = state.get("mcp_self_check") or {}
    fingerprint = config_fingerprint()
    client = last_client_activity()
    fresh = bool(check) and check.get("fingerprint") == fingerprint
    passed = fresh and check.get("status") == "ready"

    if client:
        detail = (
            f"external client connected: {client.get('client')} "
            f"{client.get('version') or ''} at {client.get('at')}"
        ).replace("  ", " ")
        if passed:
            detail += f"; MCP server self-check passed at {check.get('checked_at')}"
        status, hint = _DONE, ""
    elif passed:
        detail = (
            f"MCP server self-check passed at {check.get('checked_at')}; "
            "no external client connection observed yet"
        )
        status, hint = _DONE, "install the config below in your agent, then run one Brain tool"
    elif check and fresh:
        detail = f"MCP server self-check reported '{check.get('status')}' at {check.get('checked_at')}"
        status, hint = _ACTION, "fix the failing check and run it again"
    elif check:
        detail = "configuration changed since the last self-check — the recorded result is stale"
        status, hint = _ACTION, "run the MCP server self-check again"
    else:
        detail = "no MCP server self-check recorded yet"
        status, hint = _ACTION, "run the self-check below, then connect your agent"

    return {
        "id": "agent",
        "title": "Connect your agent",
        "status": status,
        "detail": detail,
        "hint": hint,
        "self_check": check or None,
        "self_check_fresh": fresh,
        "external_client": {
            "connected": bool(client),
            "client": (client or {}).get("client"),
            "at": (client or {}).get("at"),
        },
    }


def _index_detail(status: str, counts: Dict[str, Any]) -> str:
    """What the last run actually did, in words that survive an incremental run.

    Reporting only ``indexed`` made a completed no-change run read "0 indexed /
    0 failed of 706" next to a green tick, which looks like total failure. Files
    the run skipped because they had not changed are the normal majority, so
    they are named instead of being silently dropped from the count.
    """
    if not counts:
        return status
    read = int(counts.get("indexed") or 0)
    unchanged = int(counts.get("unchanged") or 0)
    failed = int(counts.get("failed") or 0)
    discovered = int(counts.get("discovered") or 0)
    if not read and not failed and unchanged:
        return f"{status} — all {unchanged} files already up to date, nothing changed since the last run"
    parts = [f"{read} read"]
    if unchanged:
        parts.append(f"{unchanged} unchanged")
    parts.append(f"{failed} failed")
    return f"{status} — {', '.join(parts)} of {discovered} found"


def _index_step(repo_path: Optional[Path], indexed: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if repo_path is None:
        return {
            "id": "indexed",
            "title": "Index the repository",
            "status": _BLOCKED,
            "detail": "needs a repository first",
            "hint": "",
        }
    if indexed is None:
        return {
            "id": "indexed",
            "title": "Index the repository",
            "status": _ACTION,
            "detail": "no completed index run for this repository",
            "hint": "run an index — button below or `brain index`",
        }
    status = indexed["status"]
    detail = _index_detail(status, indexed.get("files") or {})
    return {
        "id": "indexed",
        "title": "Index the repository",
        "status": _DONE if status == "completed" else _ACTION,
        "detail": detail,
        "hint": "" if status == "completed" else "re-run the index or inspect Index runs",
        "run": indexed,
    }


def _classify_pack(pack: ContextPack, index_commit: Optional[str], index_commits: frozenset[str]) -> str:
    """Bucket a repo-scoped pack: usable | historic | missing | empty | stale.

    ``usable`` describes the code as indexed right now. ``historic`` is a pack
    whose revision matches some earlier completed index run of this repository:
    no longer current, but proof a real pack was once built here — which is what
    the first-use milestone asks for.
    """
    try:
        path = Path(pack.path)
        if not path.is_file():
            return "missing"
        if path.stat().st_size == 0:
            return "empty"
    except OSError:
        return "missing"
    if not pack.repo_commit:
        return "stale"
    if index_commit and pack.repo_commit == index_commit:
        return "usable"
    if pack.repo_commit in index_commits:
        return "historic"
    return "stale"


def _first_task_step(
    packs: list[ContextPack],
    unattributed: int,
    indexed_done: bool,
    index_commit: Optional[str],
    index_commits: frozenset[str] = frozenset(),
    milestone: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    title = "Build your first context pack"
    buckets = {"usable": 0, "historic": 0, "missing": 0, "empty": 0, "stale": 0}
    for pack in packs:
        buckets[_classify_pack(pack, index_commit, index_commits)] += 1
    usable = buckets["usable"]
    older = buckets["historic"] + buckets["stale"]
    step = {
        "id": "first_task",
        "title": title,
        "packs": buckets,
        "unattributed_packs": unattributed,
        "milestone": milestone,
    }
    if not indexed_done:
        step.update(
            {
                "status": _BLOCKED,
                "detail": (
                    "a pack exists for this repository but the index never completed — re-index first"
                    if packs
                    else "needs an indexed repository first"
                ),
                "hint": "complete an index run first" if packs else "",
            }
        )
        return step
    if usable:
        plural = "" if usable == 1 else "s"
        detail = f"{usable} context pack{plural} {'matches' if usable == 1 else 'match'} the code as indexed now"
        if older:
            detail += f"; {older} describe{'s' if older == 1 else ''} older code"
        step.update({"status": _DONE, "detail": detail, "hint": ""})
        return step
    # The milestone was reached earlier: either a pack built against an earlier
    # completed index run is still on disk, or one was recorded when it was.
    # Re-indexing is routine, so this stays done and only reports freshness.
    if buckets["historic"] or milestone:
        when = f" on {milestone['at'][:10]}" if milestone and milestone.get("at") else ""
        count = older or buckets["missing"] + buckets["empty"]
        detail = f"first pack built{when}"
        if count:
            detail += f"; {count} pack{'s' if count != 1 else ''} describe older code — rebuild one when you start a task"
        else:
            detail += "; no pack for the current code — rebuild one when you start a task"
        step.update({"status": _DONE, "detail": detail, "hint": ""})
        return step
    reasons = []
    if buckets["stale"]:
        reasons.append("revision is unknown or was never indexed — rebuild for the current revision")
    if buckets["missing"] or buckets["empty"]:
        reasons.append("artifact file missing or empty — rebuild it")
    if unattributed:
        reasons.append(
            f"{unattributed} pack{'s' if unattributed != 1 else ''} from another repository "
            "or predating provenance tracking"
        )
    detail = "; ".join(reasons) if reasons else "no context pack yet"
    step.update(
        {
            "status": _ACTION,
            "detail": detail,
            "hint": "describe a real task below and build a pack — that is your first win",
        }
    )
    return step


async def collect_setup_status() -> Dict[str, Any]:
    health = await check_health()
    services = _services_step(health)

    try:
        repo_path: Optional[Path] = resolve_repo_path(settings.TARGET_REPO_PATH)
    except ValueError:
        repo_path = None

    indexed: Optional[Dict[str, Any]] = None
    index_commit: Optional[str] = None
    index_commits: frozenset[str] = frozenset()
    packs: list[ContextPack] = []
    unattributed = 0
    async with async_session_factory() as session:
        record = await get_repository_by_path(repo_path) if repo_path else None
        if record is not None:
            runs = (
                await session.execute(
                    select(IndexingRun)
                    .where(IndexingRun.repository_id == record.id)
                    .order_by(IndexingRun.id.desc())
                    .limit(10)
                )
            ).scalars().all()
            if runs:
                run = runs[0]
                indexed = {
                    "run_id": run.id,
                    "status": run.status,
                    "commit": (run.commit_hash or "")[:12],
                    "files": (run.file_counts or {}),
                }
                completed = next((r for r in runs if r.status == "completed"), None)
                index_commit = completed.commit_hash if completed else None
                index_commits = frozenset(
                    r.commit_hash for r in runs if r.status == "completed" and r.commit_hash
                )
            packs = list(
                (
                    await session.execute(
                        select(ContextPack).where(ContextPack.repository_id == record.id)
                    )
                ).scalars().all()
            )
        total_packs = int(
            (await session.execute(select(func.count()).select_from(ContextPack))).scalar()
            or 0
        )
        # Packs not provably built for the selected repository: other-repo
        # packs and unattributed legacy rows (repository_id NULL).
        unattributed = total_packs - len(packs)

    repo = _repo_step(repo_path)
    provider = _provider_step()
    indexed_step = _index_step(repo_path, indexed)
    agent = _agent_step()
    indexed_done = indexed_step["status"] == _DONE
    repo_key = str(repo_path) if repo_path else None
    milestone = first_pack_milestone(load_state(), repo_key)
    first_task = _first_task_step(
        packs, unattributed, indexed_done, index_commit, index_commits, milestone
    )
    # Stamp the milestone the first time it is observed, so it survives both a
    # re-index and the ten-run window the commit set above is read from.
    if repo_key and milestone is None and first_task["status"] == _DONE:
        record_first_pack(repo_key, index_commit)

    steps = [services, repo, provider, indexed_step, agent, first_task]
    next_step = next((s["id"] for s in steps if s["status"] != _DONE), None)
    first_use_complete = (
        services["status"] == _DONE
        and repo["status"] == _DONE
        and indexed_done
        and first_task["status"] == _DONE
    )
    return {
        "steps": steps,
        "first_use_complete": first_use_complete,
        "next_step": next_step,
        "repo_path": str(repo_path) if repo_path else None,
        "api_key_configured": bool(settings.PROJECT_BRAIN_API_KEY),
    }
