#!/usr/bin/env python3
"""Multi-user end-to-end load test of team-mode MEMORY against a real local stack.

Starts the real API (uvicorn) and worker on a throwaway PostgreSQL (pgvector) and Redis,
with mock LLM/embedding providers, bootstraps a team of N principals over 3 projects
(admins, reviewers, contributors, a reader, an OAuth client acting for a member) and drives
concurrent REST and MCP-HTTP traffic. It then checks memory invariants:

* no cross-project or cross-user (personal tier) leakage in reads, search, context packs,
  MCP replies and the activity feed;
* compare-and-swap on one decision/rule: no lost update, linear supersede chains;
* governed writes: member proposals, reviewer approval/rejection, 403 for members and
  self-approval, readers cannot write shared memory;
* consolidation G2: distinct principals, not events; one spammer never promotes;
* revocation mid-run: nothing written after the revoke persists, the restore fence holds;
* freshness envelope present and sane, origin lag through a local bare git origin;
* provenance: every item and activity event names the right principal;
* coordination leases under contention.

It writes ``report.json`` and ``REPORT.md`` under ``--out`` (default
``artifacts/team-memory-e2e/<UTC timestamp>/``) and exits 1 when an invariant is violated.
It only touches ``brain_e2e_memload*`` databases and never reads the project's ``.env``.

    py -3 scripts/team_memory_load_e2e.py --database-url postgresql://postgres:PW@127.0.0.1:5444/postgres \
        --redis-url redis://127.0.0.1:6390/0 --users 12 --ops 50

See docs/runbooks/team-memory-load-e2e.md.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import random
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
import uuid
import zlib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import team_e2e_check as base  # type: ignore[import-not-found]  # noqa: E402  (shared e2e helpers)

ROOT = base.ROOT
MAIN_DB = "brain_e2e_memload"
RESTORE_DB = "brain_e2e_memload_restore"
REPO_ALPHA = "/e2e/alpha"
REPO_BETA = "/e2e/beta"
# A second core repository that carries only consolidation evidence: G1 dedups a candidate against the active
# learnings of its repository, and the load phase fills /e2e/alpha with learnings.
REPO_MEMORY = "/e2e/alpha-memory"
REVOKE_ATTEMPTS = 10
JOB_PREFIX = "brain-e2e-memload:worker"
THROTTLE_PREFIX = "brain:team:invitation-accept:"  # apps/api/invitation_throttle.KEY_PREFIX
OAUTH_SCOPES = "team:read team:knowledge.propose core:write"  # an MCP client acting for a member
ACCEPT_LIMIT = 10  # apps/api/routers/team_invitations.ACCEPT_LIMIT (per client address per 300 s)
MARKER_RE = re.compile(r"\b(mk-(?P<project>[a-z]+)-[0-9a-f]{8}|pm-(?P<owner>[a-z0-9_]+)-[0-9a-f]{8})\b")
MAX_EXAMPLES = 5
REVOCATION_DRAIN_TIMEOUT_S = 130.0  # the HTTP timeout (120 s) plus process/thread scheduling slack


# --------------------------------------------------------------------------- pure helpers (unit-tested)

def percentile(values: Iterable[float], q: float) -> Optional[float]:
    """Nearest-rank percentile (q in 0..100); None for no samples."""
    ordered = sorted(values)
    if not ordered:
        return None
    rank = max(1, min(len(ordered), int(-(-q * len(ordered) // 100))))
    return ordered[rank - 1]


def latency_summary(samples: dict[str, list[float]], statuses: dict[str, Counter]) -> dict[str, dict]:
    """Per-endpoint count, p50/p95/p99/max in milliseconds and the status distribution."""
    out = {}
    for label in sorted(samples):
        values = samples[label]
        out[label] = {
            "count": len(values),
            "p50_ms": _ms(percentile(values, 50)), "p95_ms": _ms(percentile(values, 95)),
            "p99_ms": _ms(percentile(values, 99)), "max_ms": _ms(max(values) if values else None),
            "statuses": {str(k): v for k, v in sorted(statuses.get(label, Counter()).items())},
        }
    return out


def _ms(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value * 1000, 1)


def member_address(name: str) -> str:
    """A stable TEST-NET-2 address per member, forwarded so each member is a distinct throttle client."""
    return f"198.51.100.{zlib.crc32(name.encode()) % 250 + 1}"


def shared_marker(project: str) -> str:
    return f"mk-{project}-{secrets.token_hex(4)}"


def personal_marker(owner: str) -> str:
    return f"pm-{owner}-{secrets.token_hex(4)}"


def leaked_markers(text: str, readable_projects: set[str], owner: str, *,
                   scope_project: Optional[str] = None) -> list[str]:
    """Markers in ``text`` the reader must not see.

    A shared marker ``mk-<project>-ΓÇª`` leaks when the reader cannot read that project (or,
    with ``scope_project``, when it belongs to another project than the one the reply is
    scoped to: a context pack for alpha must not carry beta's memory). A personal marker
    ``pm-<owner>-ΓÇª`` leaks to anyone but its owner."""
    found = []
    for match in MARKER_RE.finditer(text or ""):
        project, marker_owner = match.group("project"), match.group("owner")
        if project is not None:
            if project not in readable_projects or (scope_project is not None and project != scope_project):
                found.append(match.group(0))
        elif marker_owner is not None and marker_owner != owner:
            found.append(match.group(0))
    return sorted(set(found))


def verify_chain(rows: list[dict], canonical_id: int) -> list[str]:
    """Problems in one decision/rule supersede chain (empty list when sound).

    ``rows`` are every row of the item (canonical + history) with id, version,
    supersedes_id, superseded_by_id. The chain must be linear: the canonical row
    walks back through ``supersedes_id`` over versions v, v-1, ..., 1 and every
    history row points at the canonical row."""
    problems: list[str] = []
    by_id = {int(row["id"]): row for row in rows}
    canonical = by_id.get(int(canonical_id))
    if canonical is None:
        return [f"canonical row {canonical_id} missing"]
    if canonical.get("superseded_by_id") is not None:
        problems.append(f"canonical row {canonical_id} is itself superseded")
    history = [row for row in rows if int(row["id"]) != int(canonical_id)]
    for row in history:
        if row.get("superseded_by_id") != canonical_id:
            problems.append(f"history row {row['id']} points at {row.get('superseded_by_id')}, not {canonical_id}")
    version = int(canonical.get("version") or 1)
    if len(history) != version - 1:
        problems.append(f"version {version} but {len(history)} history row(s) (expected {version - 1})")
    seen: set[int] = set()
    cursor: Optional[dict] = canonical
    expected = version
    while cursor is not None:
        if int(cursor["id"]) in seen:
            problems.append(f"cycle at row {cursor['id']}")
            break
        seen.add(int(cursor["id"]))
        if int(cursor.get("version") or 1) != expected:
            problems.append(f"row {cursor['id']} has version {cursor.get('version')}, expected {expected}")
        expected -= 1
        parent = cursor.get("supersedes_id")
        cursor = by_id.get(int(parent)) if parent is not None else None
        if parent is not None and cursor is None:
            problems.append(f"supersedes_id {parent} points outside the chain")
    if len(seen) != len(rows):
        problems.append(f"{len(rows) - len(seen)} row(s) not reachable from the canonical row")
    versions = Counter(int(row.get("version") or 1) for row in rows)
    duplicated = sorted(v for v, n in versions.items() if n > 1)
    if duplicated:
        problems.append(f"duplicated version number(s) {duplicated}")
    return problems


def cas_round_verdict(statuses: list[int]) -> dict[str, Any]:
    """One CAS round: every writer sent the same expected_version; exactly one may win."""
    counts = Counter(statuses)
    return {"winners": counts.get(200, 0), "conflicts": counts.get(409, 0),
            "other": {str(k): v for k, v in counts.items() if k not in {200, 409}},
            "ok": counts.get(200, 0) == 1 and counts.get(200, 0) + counts.get(409, 0) == len(statuses)}


# --------------------------------------------------------------------------- recording

class Metrics:
    """Thread-safe latency and status recorder keyed by endpoint label."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.samples: dict[str, list[float]] = defaultdict(list)
        self.statuses: dict[str, Counter] = defaultdict(Counter)
        self.phases: dict[str, dict[str, float]] = {}

    def record(self, label: str, status: int, seconds: float) -> None:
        with self.lock:
            self.samples[label].append(seconds)
            self.statuses[label][status] += 1

    def phase(self, name: str, seconds: float, requests: int) -> None:
        self.phases[name] = {"seconds": round(seconds, 2), "requests": requests,
                             "throughput_rps": round(requests / seconds, 1) if seconds else 0.0}

    def total(self) -> int:
        with self.lock:
            return sum(len(v) for v in self.samples.values())

    def status_distribution(self) -> dict[str, int]:
        total: Counter = Counter()
        for counter in self.statuses.values():
            total.update(counter)
        return {str(k): v for k, v in sorted(total.items())}


class Violations:
    """Invariant violations by category, with a few examples each."""

    def __init__(self, report: base.Report) -> None:
        self.lock = threading.Lock()
        self.report = report
        self.counts: Counter = Counter()
        self.examples: dict[str, list[Any]] = defaultdict(list)

    def add(self, category: str, example: Any) -> None:
        with self.lock:
            self.counts[category] += 1
            if len(self.examples[category]) < MAX_EXAMPLES:
                text = example if isinstance(example, str) else json.dumps(example, default=str, sort_keys=True)
                self.examples[category].append(base.redact(text, self.report.secrets)[:800])

    def as_dict(self) -> dict[str, Any]:
        return {category: {"count": count, "examples": self.examples[category]}
                for category, count in sorted(self.counts.items())}


@dataclass
class Member:
    name: str
    key: str = ""
    principal_id: int = 0
    is_admin: bool = False
    roles: dict[str, str] = field(default_factory=dict)  # project name -> reader|contributor|reviewer
    oauth: bool = False
    harness_key: str = ""

    def readable(self, all_projects: Iterable[str]) -> set[str]:
        """Projects whose content this member may see: a project role, for administrators too."""
        return set(all_projects) & set(self.roles)

    def writable(self) -> list[str]:
        return sorted(p for p, role in self.roles.items() if role in {"contributor", "reviewer"})

    def reviews(self) -> list[str]:
        return sorted(p for p, role in self.roles.items() if role == "reviewer")

    @property
    def kind(self) -> str:
        if self.is_admin:
            return "admin"
        if self.oauth:
            return "oauth"
        if self.reviews():
            return "reviewer"
        if self.writable():
            return "contributor"
        return "reader"


@dataclass
class LedgerEntry:
    kind: str  # decision|rule|learning_personal|learning_shared|approve|reject
    actor: str
    marker: str
    project: str
    status: int
    proposal_id: Optional[int] = None
    item_id: Optional[int] = None
    sent_at: float = 0.0


# --------------------------------------------------------------------------- stack

class Stack:
    """Processes, databases and subprocess helpers for one run."""

    def __init__(self, args: argparse.Namespace, report: base.Report, work: Path) -> None:
        self.args = args
        self.report = report
        self.work = work
        self.shared_key = report.secret(f"memload-shared-{secrets.token_urlsafe(24)}")
        self.neo4j_password = secrets.token_urlsafe(16)
        self.webhook = secrets.token_urlsafe(32)
        self.processes: list[subprocess.Popen] = []
        self.pg_password = report.secret(urlsplit(args.database_url).password or "")

    def url(self, database: str) -> str:
        return base.e2e_database_url(self.args.database_url, database)

    def admin_url(self) -> str:
        return self.url(MAIN_DB).rsplit("/", 1)[0] + "/postgres"

    def brain_env(self, database: str, **extra: str) -> dict[str, str]:
        keep_names = {"PATH", "HOME", "SYSTEMROOT", "TEMP", "TMP", "LANG", "USERPROFILE", "APPDATA",
                      "LOCALAPPDATA", "PROGRAMDATA", "COMSPEC", "PATHEXT", "SYSTEMDRIVE", "WINDIR"}
        env = {k: v for k, v in os.environ.items() if k.upper() in keep_names}
        env.update({
            "PYTHONPATH": str(ROOT),
            "PYTHONUNBUFFERED": "1",
            "ENVIRONMENT": "production",
            "BRAIN_STORAGE_MODE": "full",
            # The e2e supplies every setting it needs and must never inherit a checkout's real configuration.
            # This also applies to child uvicorn workers, unlike an in-process settings override.
            "BRAIN_DISABLE_DOTENV": "true",
            "DATABASE_URL": base.brain_url(self.url(database)),
            "REDIS_URL": self.args.redis_url,
            "WORKER_REDIS_PREFIX": JOB_PREFIX,
            "POSTGRES_PASSWORD": self.pg_password,
            "NEO4J_URI": "bolt://127.0.0.1:1",
            "NEO4J_USER": "fixture",
            "NEO4J_PASSWORD": self.neo4j_password,
            "PROJECT_BRAIN_API_KEY": self.shared_key,
            "PROJECT_BRAIN_WEBHOOK_TOKEN": self.webhook,
            "DEFAULT_LLM_PROVIDER": "mock",
            "DEFAULT_EMBEDDING_PROVIDER": "mock",
            "ALLOW_SCRATCH_REPO_ROOTS": "true",
            "PROJECT_BRAIN_GIT_REPO_PATH": str(ROOT),
            "RETRIEVAL_V2_ENABLED": "false",
            "BRAIN_AGENT_CONTEXT_V2_MODE": "off",
            "BRAIN_STALE_CONTEXT_POLICY": "strict_block",
            "BRAIN_TEAM_MODE": "false",
            "BRAIN_WEBHOOK_FETCH_SOURCE": "false",
            "LATE_INTERACTION_ENABLED": "false",
            "BRAIN_ORIGIN_POLL_ENABLED": "false",
            # The harness plays the web dashboard's role: it forwards each member's own (documentation-range)
            # address, so the per-address invitation-accept throttle sees distinct clients, as in a real team.
            "BRAIN_TRUSTED_PROXY_IPS": "127.0.0.1",
        })
        env.update(extra)
        return env

    def run(self, argv: list[str], *, env: Optional[dict[str, str]] = None, cwd: Optional[Path] = None,
            timeout: int = 300, check: bool = True, stdin: Any = None) -> subprocess.CompletedProcess:
        proc = subprocess.run(argv, env=env, cwd=str(cwd or ROOT), capture_output=True, text=True,
                              timeout=timeout, stdin=stdin)
        if check and proc.returncode != 0:
            raise RuntimeError(f"{' '.join(argv[:3])[:160]}... exited {proc.returncode}:\n"
                               f"{base.redact(proc.stdout[-2000:], self.report.secrets)}\n"
                               f"{base.redact(proc.stderr[-3000:], self.report.secrets)}")
        return proc

    def py(self, code: str, database: str = MAIN_DB, **extra: str) -> Any:
        """Run ``code`` with this tree; its last stdout line is one JSON document."""
        proc = self.run([sys.executable, "-c", code], env=self.brain_env(database, **extra))
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def cli(self, *argv: str, database: str = MAIN_DB, check: bool = True) -> subprocess.CompletedProcess:
        return self.run([sys.executable, "-m", "apps.cli.main", "team", *argv], env=self.brain_env(database),
                        check=check)

    def cli_json(self, *argv: str, database: str = MAIN_DB) -> Any:
        return json.loads(self.cli(*argv, database=database).stdout)

    # -- PostgreSQL through asyncpg (no host psql needed) --------------------------------

    def _pg(self, url: str, work: Callable[[Any], Any]) -> Any:
        import asyncpg  # type: ignore[import-untyped]

        async def go() -> Any:
            conn = await asyncpg.connect(url)
            try:
                return await work(conn)
            finally:
                await conn.close()
        return asyncio.run(go())

    def sql(self, query: str, *params: Any, database: str = MAIN_DB) -> list[dict]:
        async def work(conn: Any) -> list[dict]:
            return [dict(row) for row in await conn.fetch(query, *params)]
        return self._pg(self.url(database), work)

    def scalar(self, query: str, *params: Any, database: str = MAIN_DB) -> Any:
        async def work(conn: Any) -> Any:
            return await conn.fetchval(query, *params)
        return self._pg(self.url(database), work)

    def recreate(self, database: str) -> None:
        self.url(database)  # namespace guard

        async def work(conn: Any) -> None:
            await conn.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                               "WHERE datname=$1 AND pid<>pg_backend_pid()", database)
            await conn.execute(f'DROP DATABASE IF EXISTS "{database}"')
            await conn.execute(f'CREATE DATABASE "{database}"')
        self._pg(self.admin_url(), work)

    def drop(self, database: str) -> None:
        self.url(database)

        async def work(conn: Any) -> None:
            await conn.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                               "WHERE datname=$1 AND pid<>pg_backend_pid()", database)
            await conn.execute(f'DROP DATABASE IF EXISTS "{database}"')
        try:
            self._pg(self.admin_url(), work)
        except Exception as exc:  # noqa: BLE001 - best-effort cleanup
            print(f"warning: could not drop {database}: {type(exc).__name__}", file=sys.stderr)

    def flush_redis(self) -> int:
        return int(self.py("import asyncio, json, os, redis.asyncio as r\n"
                           "async def m():\n"
                           "    c = r.from_url(os.environ['REDIS_URL'])\n"
                           f"    keys = [k async for k in c.scan_iter(match='{JOB_PREFIX.split(':')[0]}:*')]\n"
                           f"    keys += [k async for k in c.scan_iter(match='{THROTTLE_PREFIX}*')]\n"
                           "    if keys: await c.delete(*keys)\n"
                           "    await c.aclose()\n"
                           "    return len(keys)\n"
                           "print(json.dumps(asyncio.run(m())))"))

    # -- processes ---------------------------------------------------------------------

    def start_api(self, database: str, port: int, label: str) -> base.Api:
        if port_in_use(port):
            # A stale API (e.g. orphaned uvicorn workers of an earlier run) would silently answer for this one.
            raise RuntimeError(f"port {port} already accepts connections; stop the process holding it first")
        log = open(self.work / f"api-{label}.log", "w", encoding="utf-8")
        api_base = f"http://127.0.0.1:{port}"
        env = self.brain_env(database, BRAIN_PUBLIC_URL=api_base)
        argv = [sys.executable, "-m", "uvicorn", "apps.api.main:app", "--host", "127.0.0.1", "--port", str(port),
                "--log-level", "warning"]
        if self.args.api_workers > 1:
            argv += ["--workers", str(self.args.api_workers)]
        proc = subprocess.Popen(argv, env=env, cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=sys.platform != "win32")
        self.processes.append(proc)
        api = TreeApi(api_base, proc, self.report)
        api.http.timeout = base_timeout()
        deadline = time.time() + 120
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"API {label} exited {proc.returncode}; see {log.name}")
            try:
                status, _ = api.call("GET", "/health")
                if status in {200, 503}:
                    return api
            except Exception:  # noqa: BLE001 - still starting
                pass
            time.sleep(1)
        raise RuntimeError(f"API {label} did not answer /health within 120s; see {log.name}")

    def start_worker(self, database: str = MAIN_DB) -> subprocess.Popen:
        log = open(self.work / "worker.log", "w", encoding="utf-8")
        proc = subprocess.Popen([sys.executable, "-m", "brain.workers.worker"], env=self.brain_env(database),
                                cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=sys.platform != "win32")  # stop_process_tree signals its group
        self.processes.append(proc)
        return proc

    def stop(self) -> None:
        for proc in self.processes:
            stop_process_tree(proc)
        self.processes.clear()

    # -- docker (restore fence only) ---------------------------------------------------

    def docker(self, *argv: str, stdin: Any = None, check: bool = True, timeout: int = 600
               ) -> subprocess.CompletedProcess:
        return self.run([*self.args.docker_cmd.split(), *argv], stdin=stdin, check=check, timeout=timeout,
                        env=dict(os.environ))


def base_timeout() -> Any:
    import httpx

    return httpx.Timeout(120.0, connect=10.0)


def port_in_use(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def stop_process_tree(proc: subprocess.Popen) -> None:
    """Stop a server and its children. ``uvicorn --workers N`` spawns worker processes that inherit the
    listening socket; terminating only the parent leaves them serving the port (on Windows they outlive it)."""
    if proc.poll() is None:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, check=False)
        else:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        if sys.platform == "win32":
            proc.kill()
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        proc.wait(timeout=10)


class TreeApi(base.Api):  # type: ignore[misc,name-defined]
    def close(self) -> None:
        self.http.close()
        stop_process_tree(self.proc)


# --------------------------------------------------------------------------- the run

PROJECTS = ("core", "beta", "gamma")

# name, is_admin, {project: role}, how the member joins
BASE_ROSTER: list[tuple[str, bool, dict[str, str], str]] = [
    ("owner", True, {"core": "reviewer"}, "cli-invite"),
    ("admin2", True, {"beta": "reviewer", "core": "reviewer", "gamma": "reviewer"}, "api-invite"),
    ("rev_core", False, {"core": "reviewer"}, "api-invite"),
    ("dev_core", False, {"core": "contributor"}, "api-invite"),
    ("dev_beta", False, {"beta": "contributor"}, "api-invite"),
    ("dev_both", False, {"core": "contributor", "beta": "contributor"}, "api-invite"),
    ("viewer", False, {"core": "reader"}, "api-invite"),
    ("oauth_dev", False, {"core": "contributor"}, "cli-oauth"),
    ("dev_gamma", False, {"gamma": "contributor", "core": "contributor"}, "api-invite"),
    ("rev_beta", False, {"beta": "reviewer", "gamma": "reviewer"}, "api-invite"),
    ("spammer", False, {"core": "contributor"}, "api-invite"),
    ("revokee", False, {"core": "contributor"}, "api-invite"),
]


def roster(users: int) -> list[tuple[str, bool, dict[str, str], str]]:
    """The base 12 principals plus ``users - 12`` extra members spread over the projects."""
    out = list(BASE_ROSTER)
    for index in range(max(0, users - len(BASE_ROSTER))):
        project = PROJECTS[index % 2]  # core / beta: gamma stays small for the freshness checks
        other = PROJECTS[(index + 1) % 2]
        role = "reviewer" if index % 5 == 4 else "contributor"
        roles = {project: role}
        if index % 3 == 0:
            roles[other] = "reader"
        out.append((f"extra{index:02d}", False, roles, "api-invite"))
    return out


class Run:
    def __init__(self, args: argparse.Namespace, work: Path) -> None:
        self.args = args
        self.work = work
        self.report = base.Report()
        self.stack = Stack(args, self.report, work)
        self.metrics = Metrics()
        self.violations = Violations(self.report)
        self.ledger: list[LedgerEntry] = []
        self.ledger_lock = threading.Lock()
        self.members: dict[str, Member] = {}
        self.project_ids: dict[str, str] = {}
        self.repo_paths: dict[str, str] = {"core": REPO_ALPHA, "beta": REPO_BETA}
        self.repo_ids: dict[str, int] = {}
        self.api: Optional[base.Api] = None
        self.rng = random.Random(args.seed)
        self.results: dict[str, Any] = {}
        self.freshness_states: dict[str, Counter] = defaultdict(Counter)
        self.not_exercised: list[str] = []
        self.dump_path: Optional[Path] = None
        self.revoked_at: Optional[datetime] = None
        self.local = threading.local()
        self.onboarding_retries: Counter = Counter()
        self.source_commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        self.source_dirty = bool(subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=ROOT, text=True).strip())

    # -- HTTP --------------------------------------------------------------------------

    def http(self) -> Any:
        """One httpx client per thread (connection reuse without cross-thread sharing)."""
        import httpx

        client = getattr(self.local, "client", None)
        if client is None:
            assert self.api is not None
            client = httpx.Client(base_url=self.api.base, timeout=base_timeout(), follow_redirects=False)
            self.local.client = client
        return client

    def call(self, label: str, method: str, path: str, key: Optional[str], **kwargs: Any) -> tuple[int, Any, str]:
        headers = dict(kwargs.pop("headers", {}) or {})
        if key:
            headers["X-API-Key"] = key
        started = time.perf_counter()
        try:
            response = self.http().request(method, path, headers=headers, **kwargs)
        except Exception as exc:  # noqa: BLE001 - a transport failure is a result too
            self.metrics.record(label, 0, time.perf_counter() - started)
            self.violations.add("transport_error", {"label": label, "error": f"{type(exc).__name__}: {exc}"})
            return 0, None, ""
        elapsed = time.perf_counter() - started
        self.metrics.record(label, response.status_code, elapsed)
        text = response.text
        try:
            body: Any = response.json()
        except ValueError:
            body = text[:300]
        if response.status_code >= 500:
            self.violations.add("server_error", {"label": label, "status": response.status_code,
                                                 "body": text[:300]})
        return response.status_code, body, text

    def revoke_pending_invitation(self, admin_key: str, name: str) -> None:
        status, body, _ = self.call("GET /team/invitations", "GET", "/team/invitations", admin_key)
        if status != 200 or not isinstance(body, list):
            return
        for row in body:
            if row.get("principal_name") == name and row.get("state") == "invited":
                self.call("POST /team/invitations/{id}/revoke", "POST", f"/team/invitations/{row['id']}/revoke",
                          admin_key, json={})

    def expect(self, label: str, status: int, allowed: set[int], body: Any, who: str) -> bool:
        if status in allowed or status == 0 or status >= 500:
            return status in allowed
        self.violations.add("unexpected_status", {"label": label, "who": who, "status": status,
                                                  "allowed": sorted(allowed), "detail": base.detail(body)[:200]})
        return False

    def mcp(self, label: str, key: str, tool: str, arguments: dict) -> dict:
        started = time.perf_counter()
        result = base.mcp_call(self.api.base if self.api else "", key, tool, arguments)
        status = 0 if result.get("exception") else (1 if result.get("is_error") else 200)
        self.metrics.record(label, status, time.perf_counter() - started)
        return result

    def add_ledger(self, entry: LedgerEntry) -> None:
        with self.ledger_lock:
            self.ledger.append(entry)

    def scan(self, label: str, member: Member, text: str, *, scope_project: Optional[str] = None) -> None:
        leaks = leaked_markers(text, member.readable(PROJECTS), member.name)
        if leaks and member.is_admin:
            # A team administrator holds no implicit project role (scoped reads of such a project are 403),
            # yet some unscoped listings are documented as "every scope (team administrator)". Keep these
            # apart from member leaks; the report lists them for a product decision.
            shared = [m for m in leaks if m.startswith("mk-")]
            if shared:
                with self.ledger_lock:
                    seen = self.results.setdefault("admin_unscoped_visibility", {})
                    seen[label] = seen.get(label, 0) + len(shared)
            leaks = [m for m in leaks if not m.startswith("mk-")]
        if leaks:
            self.violations.add("leak_cross_project_or_personal", {"label": label, "reader": member.name,
                                                                   "markers": leaks[:5]})
        if scope_project is not None:
            bleed = [m for m in leaked_markers(text, set(PROJECTS), member.name, scope_project=scope_project)
                     if m not in leaks and m.startswith("mk-")]
            if bleed:
                self.violations.add("context_scope_bleed", {"label": label, "reader": member.name,
                                                            "scope": scope_project, "markers": bleed[:5]})

    def note_freshness(self, project: str, body: Any, label: str) -> None:
        envelope = body.get("freshness") if isinstance(body, dict) else None
        if not isinstance(envelope, dict):
            self.freshness_states[project]["<missing>"] += 1
            self.violations.add("freshness_envelope_missing", {"label": label, "project": project,
                                                               "keys": sorted(body)[:12] if isinstance(body, dict)
                                                               else str(body)[:100]})
            return
        state = str(envelope.get("state"))
        self.freshness_states[project][state] += 1
        if state not in {"fresh", "stale_served", "unknown", "stale_blocked"}:
            self.violations.add("freshness_state_unknown_value", {"label": label, "envelope": envelope})

    # ------------------------------------------------------------------ phase 0: stack + team

    def phase_setup(self) -> None:
        r = self.report
        s = "0-setup"
        self.stack.recreate(MAIN_DB)
        self.stack.flush_redis()
        gamma = self.make_git_repos()
        self.repo_paths["gamma"] = str(gamma)
        head = self.stack.run(["git", "-C", str(gamma), "rev-parse", "HEAD"]).stdout.strip()
        seed = ("import asyncio, json, sys\n"
                "from brain.database.session import init_db, async_session_factory\n"
                "from brain.database.models import Repository\n"
                "async def m():\n"
                "    await init_db()\n"
                "    async with async_session_factory() as s:\n"
                f"        s.add_all([Repository(name='alpha', path={REPO_ALPHA!r}, indexing_status='completed',"
                " last_indexed_commit='a'*40),\n"
                f"                   Repository(name='beta', path={REPO_BETA!r}, indexing_status='completed',"
                " last_indexed_commit='b'*40),\n"
                f"                   Repository(name='memory', path={REPO_MEMORY!r}, indexing_status='completed',"
                " last_indexed_commit='c'*40),\n"
                f"                   Repository(name='gamma', path={str(gamma)!r}, indexing_status='completed',"
                f" last_indexed_commit={head!r})])\n"
                "        await s.commit()\n"
                "    return 'ok'\n"
                "print(json.dumps(asyncio.run(m())))")
        self.stack.py(seed)
        for row in self.stack.sql("SELECT id, path FROM repositories"):
            for project, path in self.repo_paths.items():
                if row["path"] == path:
                    self.repo_ids[project] = int(row["id"])
        self.stack.cli_json("init", "--name", "Memory Load E2E")
        for project in PROJECTS:
            self.project_ids[project] = self.stack.cli_json("project", "add", project)["id"]
            self.stack.cli_json("repo", "attach", "--project", project, "--repository", self.repo_paths[project])
        self.stack.cli_json("repo", "attach", "--project", "core", "--repository", REPO_MEMORY)
        self.api = self.stack.start_api(MAIN_DB, self.args.api_port, "main")
        roster_rows = roster(self.args.users)
        # First administrator from the CLI (solo refuses API invitations).
        owner_name, _, owner_roles, _ = roster_rows[0]
        created = self.stack.cli("invite", "create", "--admin", "--project", "core", "--role", owner_roles["core"],
                                 "--name", owner_name)
        token = r.secret(base.shown_secret(created.stderr))
        accepted = self.stack.cli("invite", "accept", token, "--api-url", self.api.base)
        owner = Member(owner_name, key=r.secret(base.shown_secret(accepted.stderr)), is_admin=True,
                       roles=dict(owner_roles))
        self.members[owner_name] = owner
        _, health, _ = self.call("GET /health", "GET", "/health", None)
        r.check(s, "first accepted invitation turns the Brain into team state",
                isinstance(health, dict) and health.get("mode") == "team", health)

        def join(row: tuple[str, bool, dict[str, str], str]) -> Member:
            name, is_admin, roles, how = row
            first_project = sorted(roles, key=lambda p: (roles[p] == "reader", p))[0]
            member = Member(name, is_admin=is_admin, roles=dict(roles), oauth=how == "cli-oauth")
            if how == "cli-oauth":
                for attempt in range(8):
                    try:
                        added = self.stack.cli("member", "add", "--principal", name, "--mint-key", "--project",
                                               first_project, "--role", roles[first_project])
                        break
                    except RuntimeError as exc:
                        if "deadlock detected" not in str(exc) or attempt == 7:
                            raise
                        self.onboarding_retries[f"{name} (cli member add)"] += 1
                        time.sleep(random.uniform(0.2, 1.5))
                member.harness_key = ""
                member_key = r.secret(base.shown_secret(added.stderr))
                assert self.api is not None
                grant = base.oauth_flow(self.api, member_key, OAUTH_SCOPES, f"memload {name}")
                if "access_token" not in grant:
                    raise RuntimeError(f"OAuth flow for {name} failed: {grant}")
                member.key = r.secret(grant["access_token"])
                r.secret(grant.get("refresh_token") or "")
            else:
                for attempt in range(5):
                    status, body, _ = self.call("POST /team/invitations", "POST", "/team/invitations", owner.key,
                                                json={"projects": [first_project], "role": roles[first_project],
                                                      "name": name, "is_admin": is_admin})
                    if status == 409 and attempt > 0:
                        # The server created the invitation for an earlier, timed-out attempt; its token was
                        # never seen, so revoke it and issue a fresh one.
                        self.revoke_pending_invitation(owner.key, name)
                        self.onboarding_retries[f"{name} (invitation create)"] += 1
                        continue
                    if 0 < status < 500:
                        break
                    # A transport error or 5xx is recorded by ``call``.
                    self.onboarding_retries[f"{name} (invitation create)"] += 1
                if status != 200:
                    raise RuntimeError(f"invitation for {name}: {status} {base.detail(body)}")
                invitation_id = body["invitation"]["id"]
                invite_token = r.secret(body["token"])
                # Every member accepts at the same time (a team onboarding burst). A 500 is recorded as a
                # violation by ``call``; the harness then retries so the rest of the run can proceed.
                for attempt in range(15):
                    # Retries come from a new address each time, so retrying after a 5xx never trips the
                    # per-address accept throttle (that throttle has its own check).
                    forwarded = {"X-Forwarded-For": member_address(name if attempt == 0 else f"{name}#{attempt}")}
                    status, body, _ = self.call("POST /team/invitations/accept", "POST", "/team/invitations/accept",
                                                None, json={"token": invite_token}, headers=forwarded)
                    if 0 < status < 500:
                        break
                    self.onboarding_retries[name] += 1
                    time.sleep(random.uniform(0.2, 2.0))  # jitter, so the retries do not deadlock in lockstep
                if status != 200:
                    raise RuntimeError(f"accept for {name}: {status} {base.detail(body)}")
                credential = body.get("credential")
                if credential is None:
                    # The first accept may have committed while its response timed out. The idempotent retry then
                    # deliberately returns no plaintext credential, so mint a replacement through the trusted
                    # operator path using the invitation identity already recorded on the member.
                    recover = ["member", "add", "--principal", name, "--issuer", "brain-invitation",
                               "--subject", f"invitation:{invitation_id}", "--mint-key", "--project",
                               first_project, "--role", roles[first_project]]
                    if is_admin:
                        recover.append("--admin")
                    recovered = self.stack.cli(*recover)
                    credential = base.shown_secret(recovered.stderr)
                member.key = r.secret(credential)
            return member

        with _pool(8) as pool:
            joined = list(pool.map(join, roster_rows[1:]))
        for member in joined:
            self.members[member.name] = member
        r.check(s, f"{len(joined)} concurrent invitation accepts succeed without a server error (onboarding burst)",
                not self.onboarding_retries, {"members_retried_after_5xx": dict(self.onboarding_retries)})
        if self.onboarding_retries:
            self.violations.add("concurrent_invitation_accept_5xx", {"retries": dict(self.onboarding_retries),
                                                                     "see": "api-main.log: deadlock detected on "
                                                                            "team_projects: _redeem reads the project "
                                                                            "FOR SHARE, assign_project_member then "
                                                                            "asks FOR UPDATE (lock upgrade)"})
        # The per-address accept throttle still holds under a burst: one address, ACCEPT_LIMIT + 2 bad tokens.
        burst = [self.call("POST /team/invitations/accept", "POST", "/team/invitations/accept", None,
                           json={"token": f"invalid-{i}"}, headers={"X-Forwarded-For": "203.0.113.250"})[0]
                 for i in range(ACCEPT_LIMIT + 2)]
        r.check(s, f"invitation-accept throttle answers 429 after {ACCEPT_LIMIT} attempts from one address",
                burst.count(429) >= 2 and all(code < 500 for code in burst), {"statuses": dict(Counter(burst))})
        ids = {row["name"]: int(row["id"]) for row in self.stack.sql("SELECT id, name FROM principals")}
        for member in self.members.values():
            member.principal_id = ids[member.name]
        for member in self.members.values():
            for project, role in member.roles.items():
                status, body, _ = self.call("PUT /team/projects/{id}/members/{pid}", "PUT",
                                            f"/team/projects/{self.project_ids[project]}/members/{member.principal_id}",
                                            owner.key, json={"role": role})
                if status != 200:
                    raise RuntimeError(f"role {project}/{role} for {member.name}: {status} {base.detail(body)}")
        live = self.stack.scalar("SELECT count(*) FROM team_members WHERE revoked_at IS NULL")
        r.check(s, f"{len(self.members)} members joined (CLI invite, API invitations, CLI member + OAuth)",
                live == len(self.members), {"team_members": live, "expected": len(self.members)})
        status, body, _ = self.call("GET /decisions", "GET", "/decisions", self.stack.shared_key)
        r.check(s, "shared key refused in team state", status == 403,
                {"status": status, "detail": base.detail(body)})
        self.results["roster"] = [{"name": m.name, "kind": m.kind, "admin": m.is_admin, "roles": m.roles}
                                  for m in self.members.values()]

    def make_git_repos(self) -> Path:
        """gamma: a real clone of a local bare origin, two commits behind it after a fetch."""
        git = ["git", "-c", "user.name=memload", "-c", "user.email=memload@example.invalid",
               "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false"]
        root = self.work / "git"
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True)
        origin, gamma, other = root / "gamma-origin.git", root / "gamma", root / "gamma-other"
        run = self.stack.run
        run([*git, "init", "--bare", str(origin)])
        run([*git, "clone", str(origin), str(gamma)])
        (gamma / "app.py").write_text("print('gamma v1')\n", encoding="utf-8")
        run([*git, "-C", str(gamma), "add", "app.py"])
        run([*git, "-C", str(gamma), "commit", "-m", "gamma v1"])
        run([*git, "-C", str(gamma), "push", "origin", "HEAD:main"])
        run([*git, "clone", str(origin), str(other)])
        for index in (2, 3):
            (other / "app.py").write_text(f"print('gamma v{index}')\n", encoding="utf-8")
            run([*git, "-C", str(other), "commit", "-am", f"gamma v{index}"])
        run([*git, "-C", str(other), "push", "origin", "HEAD:main"])
        run([*git, "-C", str(gamma), "fetch", "origin"])
        run([*git, "-C", str(gamma), "remote", "set-head", "origin", "main"])
        return gamma

    # ------------------------------------------------------------------ row counts

    def row_counts(self) -> dict[str, Any]:
        q = self.stack.sql
        out: dict[str, Any] = {}
        out["decisions"] = {r["k"]: r["n"] for r in q(
            "SELECT CASE WHEN superseded_by_id IS NULL THEN 'canonical' ELSE 'history' END k, count(*) n "
            "FROM decisions GROUP BY 1")}
        out["rules"] = {r["k"]: r["n"] for r in q(
            "SELECT CASE WHEN superseded_by_id IS NULL THEN 'canonical' ELSE 'history' END k, count(*) n "
            "FROM rules GROUP BY 1")}
        out["learnings"] = {r["status"]: r["n"] for r in q(
            "SELECT status, count(*) n FROM memory_learnings GROUP BY status")}
        out["proposals"] = {f"{r['kind']}:{r['state']}": r["n"] for r in q(
            "SELECT kind, state, count(*) n FROM memory_proposals GROUP BY kind, state")}
        out["episodes"] = {r["status"]: r["n"] for r in q(
            "SELECT status, count(*) n FROM memory_episodes GROUP BY status")}
        out["l1_events"] = q("SELECT count(*) n FROM harness.agent_task_events")[0]["n"]
        out["skills"] = q("SELECT count(*) n FROM memory_skills")[0]["n"]
        out["activity_events"] = q("SELECT count(*) n FROM team_activity_events")[0]["n"]
        out["leases"] = q("SELECT count(*) n FROM team_resource_leases")[0]["n"]
        return out

    # ------------------------------------------------------------------ phase 1: concurrent load

    def load_user(self, member: Member, ops: int, seed: int) -> int:
        rng = random.Random(seed)
        done = 0
        for _ in range(ops):
            try:
                self.one_op(member, rng)
            except Exception as exc:  # noqa: BLE001 - a harness bug must not hide the rest of the run
                self.violations.add("harness_exception", f"{member.name}: {type(exc).__name__}: {exc} "
                                                         f"{traceback.format_exc()[-600:]}")
            done += 1
        return done

    def one_op(self, member: Member, rng: random.Random) -> None:
        kind = member.kind
        weights = {
            "admin": [("read", 35), ("write_admin", 25), ("review", 30), ("mcp", 5), ("probe", 5)],
            "reviewer": [("read", 35), ("write_member", 25), ("review", 30), ("mcp", 5), ("probe", 5)],
            "contributor": [("read", 45), ("write_member", 40), ("mcp", 5), ("probe", 10)],
            "oauth": [("read", 50), ("write_member", 40), ("probe", 10)],
            "reader": [("read", 60), ("write_reader", 30), ("mcp", 5), ("probe", 5)],
        }[kind]
        choice = rng.choices([w[0] for w in weights], weights=[w[1] for w in weights])[0]
        getattr(self, f"op_{choice}")(member, rng)

    def op_read(self, member: Member, rng: random.Random) -> None:
        readable = sorted(member.readable(PROJECTS) & set(member.roles)) or sorted(member.readable(PROJECTS))
        project = rng.choice(readable)
        repo = self.repo_paths[project]
        pick = rng.random()
        if pick < 0.2:
            status, body, text = self.call("GET /decisions", "GET", "/decisions", member.key)
            self.expect("GET /decisions", status, {200}, body, member.name)
            self.scan("GET /decisions", member, text)
        elif pick < 0.35:
            status, body, text = self.call("GET /rules", "GET", "/rules", member.key)
            self.expect("GET /rules", status, {200}, body, member.name)
            self.scan("GET /rules", member, text)
        elif pick < 0.5:
            status, body, text = self.call("GET /learnings?repo_path", "GET", "/learnings", member.key,
                                           params={"repo_path": repo})
            self.expect("GET /learnings?repo_path", status, {200}, body, member.name)
            self.scan("GET /learnings?repo_path", member, text, scope_project=project)
        elif pick < 0.6:
            status, body, text = self.call("GET /learnings?visibility=personal", "GET", "/learnings", member.key,
                                           params={"visibility": "personal"})
            self.expect("GET /learnings?visibility=personal", status, {200}, body, member.name)
            self.scan("GET /learnings?visibility=personal", member, text)
            if status == 200 and isinstance(body, list):
                foreign = [row.get("id") for row in body if isinstance(row, dict)
                           and row.get("author_principal_id") not in {None, member.principal_id}]
                if foreign:
                    self.violations.add("personal_tier_foreign_rows", {"reader": member.name, "ids": foreign[:5]})
        elif pick < 0.75:
            label = "POST /search"
            status, body, text = self.call(label, "POST", "/search", member.key, json={
                "query": f"decision rule {project}", "repo_path": repo, "response_mode": "legacy"})
            self.expect(label, status, {200, 403} if member.oauth else {200}, body, member.name)
            if status == 200:
                self.scan(label, member, text, scope_project=project)
                self.note_freshness(project, body, label)
        elif pick < 0.9:
            label = "POST /context"
            status, body, text = self.call(label, "POST", "/context", member.key, json={
                "task_description": f"What decisions and rules apply to {project}?", "repo_path": repo})
            self.expect(label, status, {200, 403} if member.oauth else {200}, body, member.name)
            if status == 200:
                self.scan(label, member, text, scope_project=project)
                self.note_freshness(project, body, label)
        else:
            label = "GET /team/activity"
            status, body, text = self.call(label, "GET", "/team/activity", member.key, params={"limit": 100})
            self.expect(label, status, {200}, body, member.name)
            self.scan(label, member, text)

    def _decision_body(self, project: str, marker: str, actor: str) -> dict:
        return {"title": f"{marker} decision by {actor}", "repo_path": self.repo_paths[project],
                "description": f"{marker}: load-test decision recorded by {actor}", "reason": "load test"}

    def _rule_body(self, project: str, marker: str, actor: str) -> dict:
        return {"name": f"{marker} rule by {actor}", "repo_path": self.repo_paths[project],
                "description": f"{marker}: load-test rule recorded by {actor}", "rule_id": marker,
                "type": "architecture", "severity": "medium"}

    def op_write_member(self, member: Member, rng: random.Random) -> None:
        project = rng.choice(member.writable())
        pick = rng.random()
        sent = time.time()
        if pick < 0.3:
            marker = shared_marker(project)
            status, body, _ = self.call("POST /decisions (member)", "POST", "/decisions", member.key,
                                        json=self._decision_body(project, marker, member.name))
            if self.expect("POST /decisions (member)", status, {202}, body, member.name):
                self.add_ledger(LedgerEntry("decision", member.name, marker, project, status,
                                            proposal_id=int(body["proposal_id"]), sent_at=sent))
        elif pick < 0.5:
            marker = shared_marker(project)
            status, body, _ = self.call("POST /rules (member)", "POST", "/rules", member.key,
                                        json=self._rule_body(project, marker, member.name))
            if self.expect("POST /rules (member)", status, {202}, body, member.name):
                self.add_ledger(LedgerEntry("rule", member.name, marker, project, status,
                                            proposal_id=int(body["proposal_id"]), sent_at=sent))
        elif pick < 0.8:
            marker = personal_marker(member.name)
            status, body, _ = self.call("POST /learnings (personal)", "POST", "/learnings", member.key, json={
                "statement": f"{marker} personal note of {member.name} about {project}",
                "repo_scope": self.repo_paths[project], "visibility": "personal", "category": "load"})
            if self.expect("POST /learnings (personal)", status, {200}, body, member.name):
                self.add_ledger(LedgerEntry("learning_personal", member.name, marker, project, status,
                                            item_id=int(body["learning_id"]), sent_at=sent))
        else:
            marker = shared_marker(project)
            status, body, _ = self.call("POST /learnings (shared->proposal)", "POST", "/learnings", member.key,
                                        json={"statement": f"{marker} shared learning proposed by {member.name}",
                                              "repo_scope": self.repo_paths[project], "visibility": "shared",
                                              "category": "load"})
            if self.expect("POST /learnings (shared->proposal)", status, {202}, body, member.name):
                self.add_ledger(LedgerEntry("learning_shared", member.name, marker, project, status,
                                            proposal_id=int(body["proposal_id"]), item_id=int(body["learning_id"]),
                                            sent_at=sent))

    def op_write_admin(self, member: Member, rng: random.Random) -> None:
        # Team administration is not a project role: an admin writes only where it holds one.
        project = rng.choice(member.writable() or sorted(member.roles))
        pick = rng.random()
        marker = shared_marker(project)
        sent = time.time()
        if pick < 0.4:
            status, body, _ = self.call("POST /decisions (admin)", "POST", "/decisions", member.key,
                                        json=self._decision_body(project, marker, member.name))
            if self.expect("POST /decisions (admin)", status, {200}, body, member.name):
                self.add_ledger(LedgerEntry("decision_direct", member.name, marker, project, status,
                                            item_id=int(body["decision_id"]), sent_at=sent))
        elif pick < 0.7:
            status, body, _ = self.call("POST /rules (admin)", "POST", "/rules", member.key,
                                        json=self._rule_body(project, marker, member.name))
            if self.expect("POST /rules (admin)", status, {200}, body, member.name):
                self.add_ledger(LedgerEntry("rule_direct", member.name, marker, project, status, sent_at=sent))
        else:
            status, body, _ = self.call("POST /learnings (admin shared)", "POST", "/learnings", member.key, json={
                "statement": f"{marker} shared learning by admin {member.name}",
                "repo_scope": self.repo_paths[project], "category": "load"})
            if self.expect("POST /learnings (admin shared)", status, {200}, body, member.name):
                self.add_ledger(LedgerEntry("learning_direct", member.name, marker, project, status,
                                            item_id=int(body["learning_id"]), sent_at=sent))

    def op_write_reader(self, member: Member, rng: random.Random) -> None:
        project = rng.choice(sorted(member.roles))
        pick = rng.random()
        marker = shared_marker(project)
        if pick < 0.35:
            status, body, _ = self.call("POST /decisions (reader)", "POST", "/decisions", member.key,
                                        json=self._decision_body(project, marker, member.name))
            if status != 403:
                self.violations.add("reader_wrote_shared_memory", {"op": "decision", "status": status,
                                                                   "body": str(body)[:200]})
        elif pick < 0.6:
            status, body, _ = self.call("POST /rules (reader)", "POST", "/rules", member.key,
                                        json=self._rule_body(project, marker, member.name))
            if status != 403:
                self.violations.add("reader_wrote_shared_memory", {"op": "rule", "status": status,
                                                                   "body": str(body)[:200]})
        elif pick < 0.8:
            status, body, _ = self.call("POST /learnings (reader shared)", "POST", "/learnings", member.key, json={
                "statement": f"{marker} reader tries shared", "repo_scope": self.repo_paths[project],
                "visibility": "shared"})
            if status != 403:
                self.violations.add("reader_wrote_shared_memory", {"op": "learning_shared", "status": status,
                                                                   "body": str(body)[:200]})
        else:
            # The personal tier needs only READ on the scope: allowed for a reader by design.
            pmarker = personal_marker(member.name)
            status, body, _ = self.call("POST /learnings (personal)", "POST", "/learnings", member.key, json={
                "statement": f"{pmarker} reader's private note", "repo_scope": self.repo_paths[project],
                "visibility": "personal"})
            if self.expect("POST /learnings (personal)", status, {200}, body, member.name):
                self.add_ledger(LedgerEntry("learning_personal", member.name, pmarker, project, status,
                                            item_id=int(body["learning_id"]), sent_at=time.time()))

    def op_review(self, member: Member, rng: random.Random) -> None:
        projects = member.reviews()  # admins too: a project's queue needs a role on that project
        project = rng.choice(projects)
        status, body, text = self.call("GET /memory/proposals", "GET", "/memory/proposals", member.key,
                                       params={"repo_path": self.repo_paths[project], "limit": 50})
        if not self.expect("GET /memory/proposals", status, {200}, body, member.name):
            return
        self.scan("GET /memory/proposals", member, text)
        pending = [p for p in body.get("proposals", []) if p.get("author_principal_id") != member.principal_id]
        own = [p for p in body.get("proposals", []) if p.get("author_principal_id") == member.principal_id]
        if own and rng.random() < 0.2:
            target = own[0]
            status, body2, _ = self.call("POST /memory/proposals/{id}/approve (self)", "POST",
                                         f"/memory/proposals/{target['id']}/approve", member.key,
                                         json={"expected_version": target["version"]})
            if status != 403:
                self.violations.add("self_approval_not_refused", {"who": member.name, "proposal": target["id"],
                                                                  "status": status})
        if not pending:
            return
        target = rng.choice(pending)
        approve = rng.random() < 0.7
        verb = "approve" if approve else "reject"
        label = f"POST /memory/proposals/{{id}}/{verb}"
        sent = time.time()
        status, body, _ = self.call(label, "POST", f"/memory/proposals/{target['id']}/{verb}", member.key,
                                    json={"expected_version": target["version"], "reason": "load test review"})
        # 409: another reviewer decided first, or the author lost access (revocation phase).
        if self.expect(label, status, {200, 409}, body, member.name) and status == 200:
            self.add_ledger(LedgerEntry(verb, member.name, "", project, status, proposal_id=int(target["id"]),
                                        sent_at=sent))

    def op_mcp(self, member: Member, rng: random.Random) -> None:
        project = rng.choice(sorted(member.roles) or list(PROJECTS))
        repo = self.repo_paths[project]
        if rng.random() < 0.5:
            label = "MCP search_code"
            result = self.mcp(label, member.key, "search_code", {"query": f"rule {project}", "repo_path": repo})
        else:
            label = "MCP prepare_task_context"
            result = self.mcp(label, member.key, "prepare_task_context",
                              {"task_description": f"Change something in {project}", "repo_path": repo})
        if result.get("exception") or result.get("is_error"):
            self.violations.add("mcp_error", {"label": label, "who": member.name,
                                              "error": (result.get("exception") or result.get("text") or "")[:300]})
            return
        self.scan(label, member, result.get("text") or "", scope_project=project)
        try:
            payload = json.loads(result.get("text") or "{}")
        except ValueError:
            payload = {}
        if label == "MCP search_code":
            self.note_freshness(project, payload, label)

    def op_probe(self, member: Member, rng: random.Random) -> None:
        """Requests that must be refused: other projects, approvals without the Reviewer role."""
        foreign = [p for p in PROJECTS if p not in member.readable(PROJECTS)]
        pick = rng.random()
        if foreign and pick < 0.4:
            project = rng.choice(foreign)
            status, body, text = self.call("GET /learnings?repo_path (foreign)", "GET", "/learnings", member.key,
                                           params={"repo_path": self.repo_paths[project]})
            if status != 403:
                self.violations.add("foreign_read_not_refused", {"who": member.name, "project": project,
                                                                 "status": status})
            self.scan("GET /learnings?repo_path (foreign)", member, text)
        elif foreign and pick < 0.7 and member.writable():
            project = rng.choice(foreign)
            marker = shared_marker(project)
            status, body, _ = self.call("POST /decisions (foreign)", "POST", "/decisions", member.key,
                                        json=self._decision_body(project, marker, member.name))
            if status != 403:
                self.violations.add("foreign_write_not_refused", {"who": member.name, "project": project,
                                                                  "status": status})
        else:
            with self.ledger_lock:
                candidates = [e for e in self.ledger if e.proposal_id and e.kind in {"decision", "rule",
                                                                                    "learning_shared"}]
            if not candidates:
                return
            entry = rng.choice(candidates)
            reviewer_here = member.is_admin or self.members[member.name].roles.get(entry.project) == "reviewer"
            if reviewer_here and entry.actor != member.name:
                return  # a legitimate reviewer: not a probe
            status, body, _ = self.call("POST /memory/proposals/{id}/approve (no authority)", "POST",
                                        f"/memory/proposals/{entry.proposal_id}/approve", member.key,
                                        json={"expected_version": 1})
            if status not in {403, 404}:
                self.violations.add("approval_without_authority", {"who": member.name, "proposal": entry.proposal_id,
                                                                   "author": entry.actor, "status": status,
                                                                   "detail": base.detail(body)[:160]})

    def phase_load(self, label: str, users: list[Member], ops: int) -> dict[str, Any]:
        before = self.metrics.total()
        started = time.perf_counter()
        with _pool(len(users)) as pool:
            futures = [pool.submit(self.load_user, member, ops, self.rng.randrange(1 << 30)) for member in users]
            done = sum(f.result() for f in futures)
        seconds = time.perf_counter() - started
        requests = self.metrics.total() - before
        self.metrics.phase(label, seconds, requests)
        return {"users": len(users), "ops_per_user": ops, "ops_done": done, "requests": requests,
                "seconds": round(seconds, 2), "throughput_rps": round(requests / seconds, 1) if seconds else 0}

    # ------------------------------------------------------------------ phase 2: CAS races

    def phase_cas(self) -> None:
        r = self.report
        s = "2-cas"
        owner, admin2 = self.members["owner"], self.members["admin2"]
        writers = [owner, admin2]
        title = f"{shared_marker('core')} CAS target"
        status, body, _ = self.call("POST /decisions (admin)", "POST", "/decisions", owner.key, json={
            "title": title, "repo_path": REPO_ALPHA, "description": "v1", "expected_version": 0})
        r.check(s, "CAS target decision created with expected_version=0", status == 200, {"status": status})
        decision_id = int(body["decision_id"])
        winners: list[str] = []
        losers: list[str] = []
        rounds = []
        k = self.args.cas_writers
        for round_no in range(self.args.cas_rounds):
            version = int(self.stack.scalar("SELECT version FROM decisions WHERE id=$1", decision_id))
            barrier = threading.Barrier(k)

            def write(index: int, version: int = version, round_no: int = round_no,
                      barrier: threading.Barrier = barrier) -> tuple[int, str]:
                description = f"round {round_no} writer {index} {secrets.token_hex(3)}"
                barrier.wait()
                st, _, _ = self.call("POST /decisions (CAS)", "POST", "/decisions", writers[index % 2].key, json={
                    "title": title, "repo_path": REPO_ALPHA, "description": description,
                    "expected_version": version})
                return st, description

            with _pool(k) as pool:
                outcomes = list(pool.map(write, range(k)))
            verdict = cas_round_verdict([st for st, _ in outcomes])
            rounds.append(verdict)
            winners += [d for st, d in outcomes if st == 200]
            losers += [d for st, d in outcomes if st == 409]
        lost_rounds = [v for v in rounds if v["winners"] > 1]
        r.check(s, f"decision CAS: {self.args.cas_rounds} rounds x {k} concurrent writers with the same "
                   "expected_version -> exactly one winner each", all(v["ok"] for v in rounds),
                {"rounds": rounds})
        rows = self.stack.sql("SELECT id, version, description, supersedes_id, superseded_by_id FROM decisions "
                              "WHERE id=$1 OR superseded_by_id=$1", decision_id)
        problems = verify_chain(rows, decision_id)
        descriptions = Counter(row["description"] for row in rows)
        missing = [d for d in winners if descriptions.get(d, 0) == 0]
        phantom = [d for d in losers if descriptions.get(d, 0) > 0]
        r.check(s, "decision CAS: no lost update (every 200 is in the chain, no 409 is) and a linear chain",
                not problems and not missing and not phantom,
                {"chain_problems": problems, "missing_winners": missing[:5], "phantom_losers": phantom[:5],
                 "rows": len(rows), "winners": len(winners)})
        if lost_rounds or missing or problems:
            self.violations.add("lost_update_decision_cas", {"rounds_with_several_winners": len(lost_rounds),
                                                             "chain_problems": problems[:5],
                                                             "missing_winners": missing[:3]})

        # Unconditional concurrent edits (no expected_version): last writer wins, but history keeps every one.
        barrier = threading.Barrier(k)

        def blind(index: int) -> tuple[int, str]:
            description = f"blind writer {index} {secrets.token_hex(3)}"
            barrier.wait()
            st, _, _ = self.call("POST /decisions (blind)", "POST", "/decisions", writers[index % 2].key, json={
                "title": title, "repo_path": REPO_ALPHA, "description": description})
            return st, description

        with _pool(k) as pool:
            blind_out = list(pool.map(blind, range(k)))
        rows = self.stack.sql("SELECT id, version, description, supersedes_id, superseded_by_id FROM decisions "
                              "WHERE id=$1 OR superseded_by_id=$1", decision_id)
        descriptions = Counter(row["description"] for row in rows)
        accepted = [d for st, d in blind_out if st == 200]
        vanished = [d for d in accepted if descriptions.get(d, 0) == 0]
        problems = verify_chain(rows, decision_id)
        r.check(s, f"decision blind writes: {k} concurrent edits without expected_version all keep their content "
                   "in the supersede history", not vanished and not problems,
                {"statuses": dict(Counter(st for st, _ in blind_out)), "vanished": vanished[:5],
                 "chain_problems": problems[:5]})
        if vanished or problems:
            self.violations.add("lost_update_decision_blind", {"vanished": len(vanished), "accepted": len(accepted),
                                                               "chain_problems": problems[:5]})

        # Concurrent creation of a new title with expected_version=0: one row, the rest 409.
        new_title = f"{shared_marker('core')} CAS create race"
        barrier = threading.Barrier(k)

        def create(index: int) -> int:
            barrier.wait()
            st, _, _ = self.call("POST /decisions (create race)", "POST", "/decisions", writers[index % 2].key, json={
                "title": new_title, "repo_path": REPO_ALPHA, "description": f"creator {index}",
                "expected_version": 0})
            return st

        with _pool(k) as pool:
            created = list(pool.map(create, range(k)))
        canon = self.stack.scalar("SELECT count(*) FROM decisions WHERE lower(trim(title))=lower(trim($1)) "
                                  "AND superseded_by_id IS NULL", new_title)
        verdict = cas_round_verdict(created)
        r.check(s, f"decision create race: {k} concurrent creators with expected_version=0 -> one row",
                verdict["winners"] == 1 and canon == 1, {**verdict, "canonical_rows": canon})
        r.check(s, "decision create race: every losing creator gets 409 (not a server error)", verdict["ok"],
                verdict)
        if verdict["winners"] != 1 or canon != 1:
            self.violations.add("create_race_duplicate", {**verdict, "canonical_rows": canon})
        elif not verdict["ok"]:
            self.violations.add("cas_conflict_answered_500", {"what": "decision create", **verdict,
                                                              "see": "uq_decisions_normalized_title"})

        # Rules: the same CAS round on one rule.
        rule_marker = shared_marker("core")
        status, body, _ = self.call("POST /rules (admin)", "POST", "/rules", owner.key, json={
            "name": f"{rule_marker} CAS rule", "repo_path": REPO_ALPHA, "description": "v1", "rule_id": rule_marker,
            "expected_version": 0})
        # The stored key is scoped to the repository (``_find_rule``), so resolve it by name.
        rule_key = self.stack.scalar("SELECT id FROM rules WHERE name=$1 AND superseded_by_id IS NULL",
                                     f"{rule_marker} CAS rule") or rule_marker
        rule_rounds = []
        rule_winners: list[str] = []
        for round_no in range(max(1, self.args.cas_rounds // 2)):
            version = int(self.stack.scalar("SELECT version FROM rules WHERE id=$1", rule_key) or 1)
            barrier = threading.Barrier(k)

            def rule_write(index: int, version: int = version, round_no: int = round_no,
                           barrier: threading.Barrier = barrier) -> tuple[int, str]:
                description = f"rule round {round_no} writer {index} {secrets.token_hex(3)}"
                barrier.wait()
                st, _, _ = self.call("POST /rules (CAS)", "POST", "/rules", writers[index % 2].key, json={
                    "name": f"{rule_marker} CAS rule", "repo_path": REPO_ALPHA, "description": description,
                    "rule_id": rule_marker, "expected_version": version})
                return st, description

            with _pool(k) as pool:
                outcomes = list(pool.map(rule_write, range(k)))
            rule_rounds.append(cas_round_verdict([st for st, _ in outcomes]))
            rule_winners += [d for st, d in outcomes if st == 200]
        rule_rows = self.stack.sql("SELECT id, version, description, supersedes_id, superseded_by_id FROM rules "
                                   "WHERE id=$1 OR superseded_by_id=$1", rule_key)
        rule_problems = verify_chain_rules(rule_rows, rule_key)
        rule_desc = Counter(row["description"] for row in rule_rows)
        rule_missing = [d for d in rule_winners if rule_desc.get(d, 0) == 0]
        rule_lost = [v for v in rule_rounds if v["winners"] > 1]
        r.check(s, "rule CAS: at most one winner per round, no lost update, linear chain",
                not rule_lost and not rule_problems and not rule_missing,
                {"create_status": status, "rounds": rule_rounds, "chain_problems": rule_problems[:5],
                 "missing_winners": rule_missing[:3]})
        if rule_lost or rule_problems or rule_missing:
            self.violations.add("lost_update_rule_cas", {"rounds": rule_rounds, "chain_problems": rule_problems[:5]})
        rule_5xx = sum(v["other"].get("500", 0) + v["other"].get(500, 0) for v in rule_rounds)
        r.check(s, "rule CAS: every losing writer gets 409 (not a server error)",
                all(v["ok"] for v in rule_rounds), {"rounds": rule_rounds})
        if rule_5xx:
            self.violations.add("cas_conflict_answered_500", {"what": "rule", "count": rule_5xx,
                                                              "see": "rules_pkey duplicate: rule_history_id()"})

        # Member proposals on the same base version, approved concurrently by different reviewers. A fresh
        # decision, so the chain verdict is not inherited from the races above.
        title = f"{shared_marker('core')} approval target"
        status, body, _ = self.call("POST /decisions (admin)", "POST", "/decisions", owner.key, json={
            "title": title, "repo_path": REPO_ALPHA, "description": "v1", "expected_version": 0})
        if status != 200:
            raise RuntimeError(f"approval target decision: {status} {base.detail(body)}")
        decision_id = int(body["decision_id"])
        version = int(self.stack.scalar("SELECT version FROM decisions WHERE id=$1", decision_id))
        proposers = [self.members[n] for n in ("dev_core", "dev_both", "dev_gamma", "spammer") if n in self.members]
        proposal_ids = []
        for index, proposer in enumerate(proposers):
            st, pbody, _ = self.call("POST /decisions (member)", "POST", "/decisions", proposer.key, json={
                "title": title, "repo_path": REPO_ALPHA, "description": f"proposal {index} by {proposer.name}",
                "expected_version": version})
            if st == 202:
                proposal_ids.append(int(pbody["proposal_id"]))
        reviewers = [self.members[n] for n in ("owner", "admin2", "rev_core", "owner")]
        barrier = threading.Barrier(len(proposal_ids))

        def approve(index: int) -> int:
            barrier.wait()
            st, _, _ = self.call("POST /memory/proposals/{id}/approve (CAS)", "POST",
                                 f"/memory/proposals/{proposal_ids[index]}/approve",
                                 reviewers[index % len(reviewers)].key, json={"expected_version": 1})
            return st

        with _pool(len(proposal_ids)) as pool:
            approvals = list(pool.map(approve, range(len(proposal_ids))))
        final_version = int(self.stack.scalar("SELECT version FROM decisions WHERE id=$1", decision_id))
        rows = self.stack.sql("SELECT id, version, description, supersedes_id, superseded_by_id FROM decisions "
                              "WHERE id=$1 OR superseded_by_id=$1", decision_id)
        problems = verify_chain(rows, decision_id)
        verdict = cas_round_verdict(approvals)
        states = Counter(row["state"] for row in self.stack.sql(
            "SELECT state FROM memory_proposals WHERE id = ANY($1::int[])", proposal_ids))
        r.check(s, f"{len(proposal_ids)} member proposals on base v{version}, approved concurrently by different "
                   "reviewers -> one approval, the rest 409, version +1", verdict["ok"]
                and final_version == version + 1 and not problems,
                {**verdict, "base_version": version, "final_version": final_version, "proposal_states": states,
                 "chain_problems": problems[:5]})
        if verdict["winners"] > 1 or final_version > version + 1:
            self.violations.add("lost_update_concurrent_approvals", {**verdict, "base_version": version,
                                                                     "final_version": final_version})
        if verdict["winners"] == 1 and verdict["other"]:
            self.violations.add("concurrent_approval_unclean_status", verdict)

        # One proposal, two reviewers at once: one decides, the other gets 409.
        proposer = self.members["dev_core"]
        st, pbody, _ = self.call("POST /decisions (member)", "POST", "/decisions", proposer.key,
                                 json=self._decision_body("core", shared_marker("core"), proposer.name))
        pid = int(pbody["proposal_id"]) if st == 202 else 0
        pair = [self.members["owner"], self.members["rev_core"]]
        barrier = threading.Barrier(2)

        def decide(index: int) -> int:
            barrier.wait()
            verb = "approve" if index == 0 else "reject"
            st2, _, _ = self.call(f"POST /memory/proposals/{{id}}/{verb} (race)", "POST",
                                  f"/memory/proposals/{pid}/{verb}", pair[index].key, json={"expected_version": 1})
            return st2

        with _pool(2) as pool:
            decided = list(pool.map(decide, range(2)))
        state = self.stack.scalar("SELECT state FROM memory_proposals WHERE id=$1", pid)
        r.check(s, "approve vs reject of one proposal at once: exactly one 200, the other 409",
                sorted(decided) == [200, 409] and state in {"approved", "rejected"},
                {"statuses": decided, "state": state})
        self.results["cas"] = {"decision_rounds": rounds, "rule_rounds": rule_rounds, "create_race": created,
                               "concurrent_approvals": approvals, "approve_vs_reject": decided}

    # ------------------------------------------------------------------ phase 3: governance probes

    def phase_governance(self) -> None:
        r = self.report
        s = "3-governance"
        dev, viewer, rev = self.members["dev_core"], self.members["viewer"], self.members["rev_core"]
        marker = shared_marker("core")
        st, body, _ = self.call("POST /decisions (member)", "POST", "/decisions", dev.key,
                                json=self._decision_body("core", marker, dev.name))
        pid = int(body["proposal_id"]) if st == 202 else 0
        r.check(s, "member decision is a pending proposal (202), not knowledge",
                st == 202 and not self.stack.scalar("SELECT count(*) FROM decisions WHERE title LIKE $1",
                                                    f"{marker}%"), {"status": st})
        other_dev = self.members["dev_both"]
        statuses = {
            "self_approve": self.call("approve (self)", "POST", f"/memory/proposals/{pid}/approve", dev.key,
                                      json={"expected_version": 1})[0],
            "member_approve": self.call("approve (member)", "POST", f"/memory/proposals/{pid}/approve",
                                        other_dev.key, json={"expected_version": 1})[0],
            "reader_approve": self.call("approve (reader)", "POST", f"/memory/proposals/{pid}/approve",
                                        viewer.key, json={"expected_version": 1})[0],
            "member_lists_queue": self.call("GET /memory/proposals (member)", "GET", "/memory/proposals",
                                            other_dev.key, params={"repo_path": REPO_ALPHA})[0],
            "foreign_reviewer_approve": self.call("approve (foreign reviewer)", "POST",
                                                  f"/memory/proposals/{pid}/approve", self.members["rev_beta"].key,
                                                  json={"expected_version": 1})[0],
        }
        r.check(s, "self-approval, members, readers, reviewers of another project: approval refused (403)",
                all(v == 403 for v in statuses.values()), statuses)
        st, body, _ = self.call("approve (stale)", "POST", f"/memory/proposals/{pid}/approve", rev.key,
                                json={"expected_version": 7})
        r.check(s, "stale expected_version on a proposal: 409", st == 409, {"status": st})
        st, body, _ = self.call("approve", "POST", f"/memory/proposals/{pid}/approve", rev.key,
                                json={"expected_version": 1})
        row = self.stack.sql("SELECT author_principal_id, approved_by_principal_id FROM decisions WHERE title LIKE $1",
                             f"{marker}%")
        r.check(s, "reviewer approval writes the decision attributed to its author, approved_by the reviewer",
                st == 200 and row == [{"author_principal_id": dev.principal_id,
                                       "approved_by_principal_id": rev.principal_id}], {"status": st, "row": row})
        viewer_writes: dict[str, Any] = {
            "decision": self.call("POST /decisions (reader)", "POST", "/decisions", viewer.key,
                                  json=self._decision_body("core", shared_marker("core"), viewer.name))[0],
            "rule": self.call("POST /rules (reader)", "POST", "/rules", viewer.key,
                              json=self._rule_body("core", shared_marker("core"), viewer.name))[0],
            "learning_shared": self.call("POST /learnings (reader shared)", "POST", "/learnings", viewer.key,
                                         json={"statement": f"{shared_marker('core')} x", "repo_scope": REPO_ALPHA,
                                               "visibility": "shared"})[0],
            "lease": self.call("POST /team/projects/{id}/leases (reader)", "POST",
                               f"/team/projects/{self.project_ids['core']}/leases", viewer.key,
                               json={"repository_id": self.repo_ids["core"], "resource": "viewer.txt"})[0],
            "mcp_team_propose_decision": None,
        }
        mcp_result = self.mcp("MCP team_propose_decision (reader)", viewer.key, "team_propose_decision",
                              {"title": f"{shared_marker('core')} reader via MCP", "repo_path": REPO_ALPHA,
                               "description": "x"})
        viewer_writes["mcp_team_propose_decision"] = ("refused" if mcp_result.get("is_error")
                                                      or mcp_result.get("exception") else "accepted")
        r.check(s, "reader (viewer) cannot write decisions, rules, shared learnings, leases (REST) or propose via MCP",
                all(v == 403 for k, v in viewer_writes.items() if k != "mcp_team_propose_decision")
                and viewer_writes["mcp_team_propose_decision"] == "refused", viewer_writes)
        st, body, _ = self.call("POST /learnings (personal)", "POST", "/learnings", viewer.key, json={
            "statement": f"{personal_marker('viewer')} reader private note", "repo_scope": REPO_ALPHA,
            "visibility": "personal"})
        r.check(s, "reader may keep personal notes (personal tier needs READ only; by design)", st == 200,
                {"status": st})
        # MCP write surface for a contributor: team_propose_decision files a proposal attributed to her.
        mmarker = shared_marker("core")
        mcp_result = self.mcp("MCP team_propose_decision", dev.key, "team_propose_decision",
                              {"title": f"{mmarker} via MCP", "repo_path": REPO_ALPHA, "description": "MCP proposal"})
        rows = self.stack.sql("SELECT author_principal_id, state FROM memory_proposals WHERE title LIKE $1",
                              f"{mmarker}%")
        r.check(s, "MCP team_propose_decision (member key) files a pending proposal attributed to the member",
                rows == [{"author_principal_id": dev.principal_id, "state": "pending"}],
                {"rows": rows, "error": (mcp_result.get("exception") or mcp_result.get("text") or "")[:200]
                 if mcp_result.get("is_error") or mcp_result.get("exception") else None})
        if rows:
            self.add_ledger(LedgerEntry("decision", dev.name, mmarker, "core", 202,
                                        proposal_id=int(self.stack.scalar(
                                            "SELECT id FROM memory_proposals WHERE title LIKE $1", f"{mmarker}%")),
                                        sent_at=time.time()))
        oauth = self.members["oauth_dev"]
        statuses = {
            "decisions": self.call("GET /decisions", "GET", "/decisions", oauth.key)[0],
            "personal_learning": self.call("POST /learnings (personal)", "POST", "/learnings", oauth.key, json={
                "statement": f"{personal_marker('oauth_dev')} oauth note", "repo_scope": REPO_ALPHA,
                "visibility": "personal"})[0],
            "search": self.call("POST /search", "POST", "/search", oauth.key, json={
                "query": "x", "repo_path": REPO_ALPHA, "response_mode": "legacy"})[0],
            "context": self.call("POST /context", "POST", "/context", oauth.key, json={
                "task_description": "x", "repo_path": REPO_ALPHA})[0],
            "beta_read": self.call("GET /learnings?repo_path (foreign)", "GET", "/learnings", oauth.key,
                                   params={"repo_path": REPO_BETA})[0],
            "propose_decision": self.call("POST /decisions (member)", "POST", "/decisions", oauth.key,
                                          json=self._decision_body("core", shared_marker("core"), oauth.name))[0],
            "lease_without_scope": self.call("POST /team/projects/{id}/leases (oauth)", "POST",
                                             f"/team/projects/{self.project_ids['core']}/leases", oauth.key,
                                             json={"repository_id": self.repo_ids["core"],
                                                   "resource": "src/oauth.py", "ttl_seconds": 30})[0],
        }
        self.results["oauth_member_surface"] = statuses
        r.check(s, f"OAuth client for a member ({OAUTH_SCOPES}): reads and proposes in its project, refused "
                   "elsewhere and outside its granted scopes",
                statuses["decisions"] == 200 and statuses["personal_learning"] == 200 and statuses["beta_read"] == 403
                and statuses["propose_decision"] == 202 and statuses["lease_without_scope"] == 403, statuses)

    # ------------------------------------------------------------------ phase 4: consolidation / G2

    def phase_consolidation(self) -> None:
        r = self.report
        s = "4-consolidation"
        dev, owner = self.members["dev_core"], self.members["owner"]
        task_body = {"title": "memload consolidation", "goal": "feed L1 evidence", "repo_path": REPO_ALPHA}
        member_task = self.call("POST /harness/tasks (member key)", "POST", "/harness/tasks", dev.key, json=task_body)
        admin_task = self.call("POST /harness/tasks (admin key)", "POST", "/harness/tasks", owner.key, json=task_body)
        r.gap(s, "a member key can create harness tasks / record observations (L1 evidence) in team state",
              member_task[0] == 200,
              {"member_status": member_task[0], "admin_status": admin_task[0],
               "detail": base.detail(member_task[1])[:160],
               "note": "member_scopes() carries harness:read but not harness:write"})
        mcp_obs = self.mcp("MCP record_task_observation (member key)", dev.key, "record_task_observation",
                           {"task_id": str(uuid.uuid4()), "statement": "x", "category": "ops",
                            "idempotency_key": f"memload-{secrets.token_hex(6)}"})
        self.results["member_harness_write"] = {"rest_member": member_task[0], "rest_admin": admin_task[0],
                                                "mcp_observation": (mcp_obs.get("text") or
                                                                    mcp_obs.get("exception") or "")[:200]}
        # Operator path: mint harness:write credentials on the members' own principals.
        names = ["spammer", "dev_core", "dev_both", "rev_core"]
        code = ("import asyncio, json\n"
                "from brain.database.session import async_session_factory\n"
                "from brain.auth.principals import mint_credential\n"
                "from brain.team.provisioning import member_scopes\n"
                "async def m():\n"
                "    out = {}\n"
                "    async with async_session_factory() as s:\n"
                f"        for name in {names!r}:\n"
                "            raw, _ = await mint_credential(s, name=name, scopes=member_scopes(False)"
                " + ['harness:write'], kind='human')\n"
                "            out[name] = raw\n"
                "        await s.commit()\n"
                "    return out\n"
                "print(json.dumps(asyncio.run(m())))")
        keys = self.stack.py(code)
        for name, raw in keys.items():
            self.members[name].harness_key = self.report.secret(raw)

        evidence_task = {**task_body, "repo_path": REPO_MEMORY}

        def task_for(member: Member) -> str:
            for _ in range(3):  # a stalled request (transport error) is recorded by ``call``; retry it
                st, body, _ = self.call("POST /harness/tasks", "POST", "/harness/tasks", member.harness_key,
                                        json=evidence_task)
                if st != 0:
                    break
            if st != 200:
                raise RuntimeError(f"harness task for {member.name}: {st} {base.detail(body)}")
            return str(body.get("id") or body.get("task_id"))

        def observe(member: Member, task_id: str, statement: str, label: str = "POST /harness/tasks/{id}/observations"
                    ) -> int:
            st, body, _ = self.call(label, "POST", f"/harness/tasks/{task_id}/observations", member.harness_key,
                                    json={"statement": statement, "category": "ops",
                                          "idempotency_key": f"memload-{secrets.token_hex(8)}"})
            return st

        words = "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike november oscar papa " \
                "quebec romeo sierra tango uniform victor whiskey xray yankee zulu".split()

        def statement(tag: str, vocabulary: list[str]) -> str:
            return f"{tag} " + " ".join(random.Random(tag).sample(vocabulary, len(vocabulary)))

        # The mock embedder scores any two texts as near-duplicates (see the findings report), so the two claims
        # are consolidated in separate runs: one run would cluster all twelve events into one episode.
        spam_statement = statement("spamclaim", words[:13])
        pair_statement = statement("pairclaim", words[13:])
        spammer = self.members["spammer"]
        spam_task = task_for(spammer)
        spam_statuses = [observe(spammer, spam_task, spam_statement) for _ in range(6)]
        # A second task of the same principal must not count as a second principal either.
        spam_task_2 = task_for(spammer)
        spam_statuses += [observe(spammer, spam_task_2, spam_statement) for _ in range(2)]
        spam_run = self.run_consolidation(owner)
        pair_statuses = []
        for name in ("dev_core", "dev_both"):
            member = self.members[name]
            task = task_for(member)
            pair_statuses += [observe(member, task, pair_statement) for _ in range(2)]
        # Forging attribution through the generic events route: learning classifications are refused there.
        forged = self.call("POST /harness/tasks/{id}/events (forged)", "POST", f"/harness/tasks/{spam_task}/events",
                           spammer.harness_key, json={
                               "event_type": "observed_lesson", "classification": "learning",
                               "idempotency_key": f"memload-{secrets.token_hex(8)}", "expected_task_version": 0,
                               "payload": {"statement": spam_statement, "author_principal_id": dev.principal_id,
                                           "reported_validation": True}})
        r.check(s, "generic /events refuses learning classifications (no forged author/validation reaches L1)",
                forged[0] in {400, 403, 409, 422}, {"status": forged[0], "detail": base.detail(forged[1])[:200]})
        r.check(s, "L1 observations accepted (spammer x8 over two tasks, two members x2 each)",
                all(st == 200 for st in spam_statuses + pair_statuses),
                {"spam": dict(Counter(spam_statuses)), "pair": dict(Counter(pair_statuses))})
        pair_run = self.run_consolidation(owner)
        self.results["consolidation_run"] = {"single_principal": spam_run, "two_principals": pair_run}
        episodes = self.stack.sql("SELECT id, status, author_principal_id, gate_reasons, promoted_to_learning_id, "
                                  "distilled_summary FROM memory_episodes ORDER BY id")
        spam_eps = [e for e in episodes if "spamclaim" in str(e.get("distilled_summary") or "")]
        pair_eps = [e for e in episodes if "pairclaim" in str(e.get("distilled_summary") or "")]
        spam_active = self.stack.scalar("SELECT count(*) FROM memory_learnings WHERE statement LIKE '%spamclaim%' "
                                        "AND status='active'")
        pair_learning = self.stack.sql("SELECT id, status, author_principal_id, evidence FROM memory_learnings "
                                       "WHERE statement LIKE '%pairclaim%'")
        spam_proposals = self.stack.sql("SELECT id, state, author_principal_id, version FROM memory_proposals "
                                        "WHERE kind='learning' AND payload::text LIKE '%spamclaim%'")
        r.check(s, "G2: one principal x8 events over two tasks never becomes active shared memory",
                spam_active == 0 and bool(spam_eps), {"episodes": _brief(spam_eps), "active": spam_active,
                                                      "proposals": spam_proposals})
        r.check(s, "G2: the single-principal claim is proposed for independent review, attributed to the spammer",
                any(p["author_principal_id"] == spammer.principal_id and p["state"] == "pending"
                    for p in spam_proposals), {"proposals": spam_proposals})
        if spam_active:
            self.violations.add("g2_single_principal_promoted", {"episodes": _brief(spam_eps)})
        pair_ok = bool(pair_eps) and any(e["status"] in {"promoted", "proposed", "pending"} for e in pair_eps)
        r.check(s, "G2: the same claim from two distinct principals passes G2 (promoted or queued for approval)",
                pair_ok, {"episodes": _brief(pair_eps), "learnings": pair_learning})
        if pair_learning:
            authors = {row["author_principal_id"] for row in pair_learning}
            r.check(s, "multi-principal learning is unattributed (author NULL), its evidence names both principals",
                    authors == {None} and all(
                        {dev.principal_id, self.members["dev_both"].principal_id}
                        <= {int(e["author_principal_id"]) for e in _json(row["evidence"])
                            if isinstance(e, dict) and e.get("author_principal_id") is not None}
                        for row in pair_learning), {"learnings": pair_learning})
        if spam_proposals:
            proposal = spam_proposals[0]
            statuses = {
                "spammer_self": self.call("approve (evidence author)", "POST",
                                          f"/memory/proposals/{proposal['id']}/approve", spammer.key,
                                          json={"expected_version": proposal["version"]})[0],
                "contributor": self.call("approve (member)", "POST", f"/memory/proposals/{proposal['id']}/approve",
                                         dev.key, json={"expected_version": proposal["version"]})[0],
            }
            st, _, _ = self.call("reject", "POST", f"/memory/proposals/{proposal['id']}/reject",
                                 self.members["rev_core"].key, json={"expected_version": proposal["version"],
                                                                     "reason": "single source"})
            statuses["independent_reviewer_reject"] = st
            ep_state = [e["status"] for e in self.stack.sql(
                "SELECT status FROM memory_episodes WHERE distilled_summary LIKE '%spamclaim%'")]
            r.check(s, "consolidated proposal: evidence author and members refused, an independent reviewer rejects "
                       "and the episode closes", statuses["spammer_self"] == 403 and statuses["contributor"] == 403
                    and st == 200 and "proposed" not in ep_state, {**statuses, "episode_states": ep_state})

    def run_consolidation(self, admin: Member) -> dict[str, Any]:
        """Through the queue and the real worker; a direct run if the worker cannot finish it."""
        worker = self.stack.start_worker()
        st, body, _ = self.call("POST /jobs/memory-consolidation", "POST", "/jobs/memory-consolidation", admin.key,
                                json={"require_approval": False})
        outcome: dict[str, Any] = {"enqueue_status": st}
        job_id = body.get("job_id") if isinstance(body, dict) else None
        deadline = time.time() + self.args.worker_timeout
        job: Any = None
        while job_id and time.time() < deadline:
            js, job, _ = self.call("GET /jobs/{id}", "GET", f"/jobs/{job_id}", admin.key,
                                   params={"include_result": "true"})
            if isinstance(job, dict) and job.get("status") in {"completed", "failed", "dead", "cancelled"}:
                break
            if worker.poll() is not None:
                break
            time.sleep(1)
        outcome["job"] = {k: job.get(k) for k in ("status", "error", "result")} if isinstance(job, dict) else job
        outcome["via"] = "worker"
        if not (isinstance(job, dict) and job.get("status") == "completed"):
            outcome["via"] = "direct"
            self.not_exercised.append("consolidation through the worker queue did not complete within "
                                      f"{self.args.worker_timeout}s (job={outcome['job']}); ran run_consolidation() "
                                      "directly instead")
            outcome["direct"] = self.stack.py("import asyncio, json\n"
                                              "from brain.memory.consolidation import run_consolidation\n"
                                              "print(json.dumps(asyncio.run(run_consolidation()), default=str))")
        if worker.poll() is None:
            worker.terminate()
            try:
                worker.wait(timeout=20)
            except subprocess.TimeoutExpired:
                worker.kill()
        return outcome

    # ------------------------------------------------------------------ phase 5: freshness

    def phase_freshness(self) -> None:
        r = self.report
        s = "5-freshness"
        member = self.members["dev_gamma"]
        repo = self.repo_paths["gamma"]
        st, body, _ = self.call("POST /search", "POST", "/search", member.key, json={
            "query": "gamma", "repo_path": repo, "response_mode": "legacy"})
        envelope = body.get("freshness") if isinstance(body, dict) else None
        r.check(s, "gamma (/search): envelope reports the local bare origin 2 commits ahead of the indexed commit",
                st == 200 and isinstance(envelope, dict) and _origin_behind(envelope) == 2,
                {"status": st, "freshness": envelope})
        st2, body2, _ = self.call("POST /context", "POST", "/context", member.key, json={
            "task_description": "change gamma", "repo_path": repo})
        envelope2 = body2.get("freshness") if isinstance(body2, dict) else None
        r.check(s, "gamma (/context): origin ahead -> not served as fresh (stale_served or blocked)",
                st2 in {200, 409, 423} and isinstance(envelope2, dict) and envelope2.get("state") != "fresh",
                {"status": st2, "freshness": envelope2})
        self.results["freshness_gamma"] = {"search": envelope, "context": envelope2}
        r.check(s, "every /search, /context and MCP search_code reply in the load carried an envelope",
                all("<missing>" not in c for c in self.freshness_states.values()),
                {p: dict(c) for p, c in self.freshness_states.items()})

    # ------------------------------------------------------------------ phase 6: leases

    def phase_leases(self) -> None:
        r = self.report
        s = "6-leases"
        pid, repo_id = self.project_ids["core"], self.repo_ids["core"]
        # Human core contributors (the OAuth grant holds no task/lease scope, by design).
        contenders = [m for m in self.members.values()
                      if m.roles.get("core") == "contributor" and not m.oauth and m.name != "revokee"]
        k = max(2, self.args.lease_contenders)
        barrier = threading.Barrier(k)
        resource = f"src/memload/{secrets.token_hex(3)}.py"

        def claim(index: int) -> tuple[int, Any, str]:
            barrier.wait()
            member = contenders[index % len(contenders)]
            st, body, _ = self.call("POST /team/projects/{id}/leases", "POST", f"/team/projects/{pid}/leases",
                                    member.key, json={"repository_id": repo_id, "resource": resource,
                                                      "ttl_seconds": 60})
            return st, body, member.name

        with _pool(k) as pool:
            claims = list(pool.map(claim, range(k)))
        statuses = Counter(st for st, _, _ in claims)
        # A holder repeating its claim gets its own live lease back (200, same id; by design), so the
        # invariant is one lease id and one holder, not one 200.
        granted = {(body["lease"]["id"], name) for st, body, name in claims if st == 200 and isinstance(body, dict)}
        active = self.stack.scalar("SELECT count(*) FROM team_resource_leases WHERE resource=$1 AND released_at IS NULL "
                                   "AND expires_at > now()", resource)
        r.check(s, f"{k} concurrent claims of one resource: exactly one lease and one holder, the rest 409",
                len(granted) == 1 and statuses.get(200, 0) + statuses.get(409, 0) == k and active == 1,
                {"statuses": dict(statuses), "active_rows": active, "granted": sorted(granted)})
        if len(granted) > 1 or (active or 0) > 1:
            self.violations.add("lease_double_grant", {"statuses": dict(statuses), "active_rows": active,
                                                       "granted": sorted(granted)})
        winner = next(((body, name) for st, body, name in claims if st == 200), None)
        if winner is None:
            return
        lease = winner[0]["lease"]
        holder = self.members[winner[1]]
        other = next(m for m in contenders if m.name != holder.name)
        overlap = self.call("POST /team/projects/{id}/leases (overlap)", "POST", f"/team/projects/{pid}/leases",
                            other.key, json={"repository_id": repo_id, "resource": "src/memload", "ttl_seconds": 60})
        stale = self.call("POST /team/projects/{id}/leases/{lid}/renew", "POST",
                          f"/team/projects/{pid}/leases/{lease['id']}/renew", holder.key,
                          json={"fencing_token": int(lease["fencing_token"]) + 5})
        foreign_release = self.call("POST /team/projects/{id}/leases/{lid}/release", "POST",
                                    f"/team/projects/{pid}/leases/{lease['id']}/release", other.key,
                                    json={"fencing_token": int(lease["fencing_token"])})
        release = self.call("POST /team/projects/{id}/leases/{lid}/release", "POST",
                            f"/team/projects/{pid}/leases/{lease['id']}/release", holder.key,
                            json={"fencing_token": int(lease["fencing_token"])})
        takeover = self.call("POST /team/projects/{id}/leases", "POST", f"/team/projects/{pid}/leases", other.key,
                             json={"repository_id": repo_id, "resource": resource, "ttl_seconds": 60})
        new_token = takeover[1].get("lease", {}).get("fencing_token") if isinstance(takeover[1], dict) else None
        r.check(s, "nested-path overlap 409, stale fencing token refused, non-holder cannot release, "
                   "release + takeover bumps the fencing token",
                overlap[0] == 409 and stale[0] in {409, 403} and foreign_release[0] in {403, 409}
                and release[0] == 200 and takeover[0] == 200 and (new_token or 0) > int(lease["fencing_token"]),
                {"overlap": overlap[0], "stale_renew": stale[0], "foreign_release": foreign_release[0],
                 "release": release[0], "takeover": takeover[0], "token_before": lease["fencing_token"],
                 "token_after": new_token})
        # Churn: every contender claims/releases random resources from a small pool for a while.
        pool_resources = [f"src/churn/{i}.py" for i in range(4)]
        churn_rounds = self.args.lease_churn
        held_max: Counter = Counter()

        def churn(member: Member, seed: int) -> Counter:
            rng = random.Random(seed)
            out: Counter = Counter()
            for _ in range(churn_rounds):
                res = rng.choice(pool_resources)
                st, body, _ = self.call("POST /team/projects/{id}/leases (churn)", "POST",
                                        f"/team/projects/{pid}/leases", member.key,
                                        json={"repository_id": repo_id, "resource": res, "ttl_seconds": 30})
                out[str(st)] += 1
                if st == 200:
                    lease_row = body["lease"]
                    rel = self.call("POST /team/projects/{id}/leases/{lid}/release (churn)", "POST",
                                    f"/team/projects/{pid}/leases/{lease_row['id']}/release", member.key,
                                    json={"fencing_token": int(lease_row["fencing_token"])})
                    out[f"release_{rel[0]}"] += 1
            return out

        with _pool(len(contenders)) as pool:
            churned = [f.result() for f in [pool.submit(churn, m, i) for i, m in enumerate(contenders)]]
        for counter in churned:
            held_max.update(counter)
        overlapping = self.stack.sql(
            "SELECT a.id a_id, b.id b_id, a.resource FROM team_resource_leases a JOIN team_resource_leases b "
            "ON a.resource=b.resource AND a.id<b.id AND a.repository_id=b.repository_id "
            "AND a.acquired_at < coalesce(b.released_at, b.expires_at) "
            "AND b.acquired_at < coalesce(a.released_at, a.expires_at) "
            "WHERE a.resource LIKE 'src/churn/%' LIMIT 5")
        r.check(s, "lease churn: no two leases on one resource ever overlapped in time (fenced, serialized)",
                not overlapping, {"statuses": dict(held_max), "overlapping": overlapping})
        if overlapping:
            self.violations.add("lease_overlap_in_time", overlapping)
        self.results["leases"] = {"contended": dict(statuses), "churn": dict(held_max)}

    # ------------------------------------------------------------------ phase 7: provenance + activity

    def phase_provenance(self) -> None:
        r = self.report
        s = "7-provenance"
        ids = {m.name: m.principal_id for m in self.members.values()}
        with self.ledger_lock:
            ledger = list(self.ledger)
        proposals = {int(row["id"]): row for row in self.stack.sql(
            "SELECT id, kind, state, author_principal_id, reviewed_by_principal_id, result_key, title, payload::text p "
            "FROM memory_proposals")}
        wrong: list[dict] = []
        missing: list[dict] = []
        for entry in ledger:
            if entry.proposal_id and entry.kind in {"decision", "rule", "learning_shared"}:
                row = proposals.get(entry.proposal_id)
                if row is None:
                    missing.append({"proposal": entry.proposal_id, "actor": entry.actor})
                elif row["author_principal_id"] != ids[entry.actor] or entry.marker not in (row["title"] + row["p"]):
                    wrong.append({"proposal": entry.proposal_id, "expected": entry.actor,
                                  "author": row["author_principal_id"]})
        for entry in [e for e in ledger if e.kind in {"approve", "reject"}]:
            row = proposals.get(entry.proposal_id or 0)
            if row is None or row["reviewed_by_principal_id"] != ids[entry.actor]:
                wrong.append({"proposal": entry.proposal_id, "reviewer_expected": entry.actor,
                              "reviewed_by": row and row["reviewed_by_principal_id"]})
        r.check(s, f"{len(ledger)} ledger entries: every accepted proposal exists with its author and every review "
                   "names its reviewer", not wrong and not missing, {"wrong": wrong[:5], "missing": missing[:5]})
        if wrong or missing:
            self.violations.add("provenance_mismatch", {"wrong": wrong[:5], "missing": missing[:5]})

        # Approved knowledge: author = proposal author, approved_by = reviewer.
        decision_rows = self.stack.sql("SELECT id, title, author_principal_id, approved_by_principal_id, "
                                       "updated_by_principal_id FROM decisions WHERE superseded_by_id IS NULL")
        by_marker = {m.group(0): row for row in decision_rows for m in [MARKER_RE.search(row["title"] or "")] if m}
        bad: list[dict] = []
        for entry in ledger:
            if entry.kind == "decision_direct":
                row = by_marker.get(entry.marker)
                if row is None or row["author_principal_id"] != ids[entry.actor]:
                    bad.append({"marker": entry.marker, "expected": entry.actor, "row": row})
            if entry.kind == "decision" and entry.proposal_id:
                prop = proposals.get(entry.proposal_id)
                row = by_marker.get(entry.marker)
                if prop and prop["state"] == "approved" and row is not None:
                    if row["author_principal_id"] != ids[entry.actor] or \
                            row["approved_by_principal_id"] != prop["reviewed_by_principal_id"]:
                        bad.append({"marker": entry.marker, "expected_author": entry.actor, "row": row,
                                    "reviewer": prop["reviewed_by_principal_id"]})
                if prop and prop["state"] != "approved" and row is not None:
                    bad.append({"marker": entry.marker, "unapproved_but_published": prop["state"]})
        r.check(s, "decisions: direct writes attributed to the admin; approved proposals to their author and reviewer; "
                   "nothing unapproved published", not bad, {"bad": bad[:5]})
        if bad:
            self.violations.add("knowledge_attribution", bad[:5])

        learning_rows = {m.group(0): row for row in self.stack.sql(
            "SELECT id, statement, status, author_principal_id FROM memory_learnings")
            for m in [MARKER_RE.search(row["statement"] or "")] if m}
        bad_learning = []
        for entry in ledger:
            if entry.kind in {"learning_personal", "learning_direct", "learning_shared"}:
                row = learning_rows.get(entry.marker)
                if row is None or row["author_principal_id"] != ids[entry.actor]:
                    bad_learning.append({"marker": entry.marker, "expected": entry.actor, "row": row})
                elif entry.kind == "learning_personal" and row["status"] != "personal":
                    bad_learning.append({"marker": entry.marker, "personal_now": row["status"]})
        r.check(s, "learnings (personal, admin shared, member shared proposals): every write persisted and attributed",
                not bad_learning, {"bad": bad_learning[:5]})
        if bad_learning:
            self.violations.add("learning_attribution", bad_learning[:5])

        # Activity feed rows for reviews name the reviewer; proposals name the author.
        feed = self.stack.sql("SELECT kind, actor_principal_id, details::text d FROM team_activity_events "
                              "WHERE kind LIKE 'memory.%'")
        feed_bad = []
        for row in feed:
            try:
                pid = int(json.loads(row["d"]).get("proposal_id"))
            except (TypeError, ValueError):
                continue
            prop = proposals.get(pid)
            if prop is None:
                continue
            expected = prop["author_principal_id"] if row["kind"] == "memory.proposed" else \
                prop["reviewed_by_principal_id"]
            if row["actor_principal_id"] != expected:
                feed_bad.append({"kind": row["kind"], "proposal": pid, "actor": row["actor_principal_id"],
                                 "expected": expected})
        decided = sum(1 for p in proposals.values() if p["state"] in {"approved", "rejected"})
        approved_events = sum(1 for row in feed if row["kind"] in {"memory.approved", "memory.rejected"})
        r.check(s, "activity feed: memory.* events name the right actor and every decided proposal has one",
                not feed_bad and approved_events >= decided,
                {"bad": feed_bad[:5], "events": len(feed), "decided_proposals": decided,
                 "review_events": approved_events})
        if feed_bad:
            self.violations.add("activity_actor_mismatch", feed_bad[:5])
        # API view of the feed: each member sees only readable projects (leak scan) and correct actor names.
        api_bad = []
        for member in self.members.values():
            st, body, text = self.call("GET /team/activity", "GET", "/team/activity", member.key,
                                       params={"limit": 200})
            if st != 200:
                continue
            self.scan("GET /team/activity (provenance)", member, text)
            for event in body.get("events", []):
                actor = event.get("actor") or {}
                if actor.get("principal_id") and actor.get("name") and ids.get(actor["name"]) not in {None,
                                                                                                   actor["principal_id"]}:
                    api_bad.append({"reader": member.name, "event": event.get("kind"), "actor": actor})
        r.check(s, "GET /team/activity for every member: actor id and name agree", not api_bad, {"bad": api_bad[:5]})

    # ------------------------------------------------------------------ phase 8: revocation + restore fence

    def snapshot_dump(self) -> None:
        if not self.args.docker_cmd or not self.args.pg_container:
            self.not_exercised.append("restore fence: needs --docker-cmd and --pg-container (pg_dump/psql run in "
                                      "the PostgreSQL container)")
            return
        path = self.work / "snapshot.sql"
        with open(path, "w", encoding="utf-8") as handle:
            proc = subprocess.run([*self.args.docker_cmd.split(), "exec", self.args.pg_container, "pg_dump", "-U",
                                   "postgres", "-d", MAIN_DB], stdout=handle, stderr=subprocess.PIPE, text=True,
                                  timeout=600)
        if proc.returncode != 0:
            self.not_exercised.append(f"restore fence: pg_dump failed ({proc.stderr.strip()[-200:]})")
            return
        self.dump_path = path

    def phase_revocation(self) -> None:
        r = self.report
        s = "8-revocation"
        revokee, owner = self.members["revokee"], self.members["owner"]
        events: list[dict] = []
        stop = threading.Event()
        revoke_done = threading.Event()
        revoke_completed_at = [0.0]

        def hammer(worker: int) -> None:
            rng = random.Random(worker)
            while not stop.is_set():
                sent = time.time()
                op = rng.choice(["personal", "decision", "read", "learning_shared"])
                if op == "personal":
                    st, body, _ = self.call("revokee POST /learnings", "POST", "/learnings", revokee.key, json={
                        "statement": f"{personal_marker('revokee')} during revocation", "repo_scope": REPO_ALPHA,
                        "visibility": "personal"})
                elif op == "decision":
                    st, body, _ = self.call("revokee POST /decisions", "POST", "/decisions", revokee.key,
                                            json=self._decision_body("core", shared_marker("core"), "revokee"))
                elif op == "learning_shared":
                    st, body, _ = self.call("revokee POST /learnings shared", "POST", "/learnings", revokee.key, json={
                        "statement": f"{shared_marker('core')} revokee shared", "repo_scope": REPO_ALPHA,
                        "visibility": "shared"})
                else:
                    st, body, _ = self.call("revokee GET /decisions", "GET", "/decisions", revokee.key)
                events.append({"op": op, "sent": sent, "received": time.time(), "status": st})

        threads = [threading.Thread(target=hammer, args=(i,), daemon=True) for i in range(self.args.revoke_threads)]
        for thread in threads:
            thread.start()
        time.sleep(self.args.revoke_after)
        # The revocation is retried while she keeps writing: a failed attempt rolls back and leaves her key live,
        # which is itself the finding; the later checks need a revocation that took effect.
        attempts: list[dict] = []
        st, body = 0, None
        for _ in range(REVOKE_ATTEMPTS):
            st, body, _ = self.call("DELETE /team/projects/members/{id}", "DELETE",
                                    f"/team/projects/members/{revokee.principal_id}", owner.key)
            attempts.append({"status": st, "detail": base.detail(body)[:120]})
            if st == 200:
                break
            time.sleep(0.3)
        revoke_completed_at[0] = time.time()
        revoke_done.set()
        time.sleep(self.args.revoke_tail)
        stop.set()
        drain_deadline = time.monotonic() + REVOCATION_DRAIN_TIMEOUT_S
        for thread in threads:
            thread.join(timeout=max(0.0, drain_deadline - time.monotonic()))
        alive = [thread.name for thread in threads if thread.is_alive()]
        r.check(s, "every revocation hammer request drained before persisted writes were inspected", not alive,
                {"alive_threads": alive, "timeout_s": REVOCATION_DRAIN_TIMEOUT_S})
        if alive:
            self.violations.add("harness_exception", {"revocation_hammer_threads_still_alive": alive})
            raise RuntimeError("revocation hammer requests outlived the HTTP timeout; persisted-write check unsafe")
        r.check(s, "admin revokes a member while she is writing: the first attempt succeeds",
                attempts[0]["status"] == 200, {"attempts": attempts})
        if attempts[0]["status"] != 200:
            self.violations.add("revoke_fails_while_member_active", {
                "attempts": attempts,
                "see": "revoke_member locks team_members FOR UPDATE then updates api_credentials, while every "
                       "request of the member locks api_credentials FOR SHARE then team_members FOR SHARE "
                       "(brain/team/authorization.py fresh/human_member): a lock-order inversion, the revoke "
                       "deadlocks, rolls back and the key stays live"})
        revoked_at = self.stack.scalar("SELECT revoked_at FROM team_members WHERE principal_id=$1",
                                       revokee.principal_id)
        self.revoked_at = revoked_at
        self.results["revocation_attempts"] = attempts
        if st != 200 or revoked_at is None:
            r.check(s, f"revocation took effect within {REVOKE_ATTEMPTS} attempts", False, {"attempts": attempts})
            self.not_exercised.append("post-revocation checks: the member could not be revoked under load")
            return
        after = [e for e in events if e["sent"] > revoke_completed_at[0]]
        honored_after = [e for e in after if 200 <= e["status"] < 300]
        unexpected_after = [e for e in after if e["status"] != 401 and not 200 <= e["status"] < 300]
        before_ok = [e for e in events if e["received"] < revoke_completed_at[0] - 0.5 and e["status"] in {200, 202}]
        r.check(s, f"{len(after)} requests sent after the revoke returned: all 401 "
                   f"({len(before_ok)} accepted before it)", after and not honored_after and not unexpected_after,
                {"after_statuses": dict(Counter(e["status"] for e in after)),
                 "honored_examples": honored_after[:3], "unexpected_examples": unexpected_after[:3]})
        if honored_after:
            self.violations.add("request_honoured_after_revoke", honored_after[:5])
        if unexpected_after:
            self.violations.add("post_revoke_request_error", unexpected_after[:5])
        late = {
            "learnings": self.stack.sql("SELECT id, status, created_at FROM memory_learnings WHERE "
                                        "author_principal_id=$1 AND created_at > $2", revokee.principal_id, revoked_at),
            "proposals": self.stack.sql("SELECT id, state, created_at FROM memory_proposals WHERE "
                                        "author_principal_id=$1 AND created_at > $2", revokee.principal_id, revoked_at),
            "decisions": self.stack.sql("SELECT id, created_at FROM decisions WHERE author_principal_id=$1 AND "
                                        "created_at > $2", revokee.principal_id, revoked_at),
        }
        r.check(s, "in-flight writes are fenced: nothing authored by the revoked member is dated after revoked_at",
                not any(late.values()), {k: v[:3] for k, v in late.items()})
        if any(late.values()):
            self.violations.add("write_persisted_after_revoke", {
                **{k: v[:3] for k, v in late.items()}, "revoked_at": str(revoked_at),
                "see": "routes authorize in their own short transactions, then the store writes in another one "
                       "(apps/api/routers/core_memory.py create_learning): a request authorized just before the "
                       "revoke commits its write after it"})
        pending = self.stack.sql("SELECT id, state, created_at FROM memory_proposals WHERE author_principal_id=$1 "
                                 "AND state='pending'", revokee.principal_id)
        states = Counter(row["state"] for row in self.stack.sql(
            "SELECT state FROM memory_proposals WHERE author_principal_id=$1", revokee.principal_id))
        r.check(s, "every pending proposal of the revoked member is withdrawn (none left pending, incl. in-flight)",
                not pending, {"states": dict(states), "pending": pending[:3]})
        if pending:
            self.violations.add("pending_proposal_survives_revoke", pending[:5])
        personal = self.stack.scalar("SELECT count(*) FROM memory_learnings WHERE author_principal_id=$1 AND "
                                     "status='personal'", revokee.principal_id)
        r.check(s, "revoked member's personal-tier learnings are retired with her membership",
              personal == 0, {"personal_rows_kept": personal,
                              "note": "kept rows are invisible (only their author reads them) but not removed"})
        statuses = {"rest": self.call("GET /decisions", "GET", "/decisions", revokee.key)[0],
                    "mcp": base.mcp_status(self.api.base if self.api else "", revokee.key)}
        r.check(s, "revoked key dead on REST and MCP", all(v == 401 for v in statuses.values()), statuses)
        self.results["revocation"] = {"requests": len(events), "after_revoke": len(after),
                                      "statuses": dict(Counter(e["status"] for e in events)),
                                      "proposal_states": dict(states)}

    def phase_restore_fence(self) -> None:
        r = self.report
        s = "9-restore-fence"
        if self.dump_path is None:
            return
        self.stack.recreate(RESTORE_DB)
        fence = (ROOT / "scripts" / "team_restore_fence.sql").read_text(encoding="utf-8")
        script = self.work / "restore-input.sql"
        script.write_text(self.dump_path.read_text(encoding="utf-8") + "\n" + fence, encoding="utf-8")
        with open(script, "r", encoding="utf-8") as handle:
            proc = self.stack.docker("exec", "-i", self.args.pg_container, "psql", "-X", "-q", "-U", "postgres", "-d",
                                     RESTORE_DB, "--single-transaction", "-v", "ON_ERROR_STOP=1", "-v",
                                     "fence_reason=memload e2e snapshot before revocation", stdin=handle, check=False)
        r.check(s, "snapshot restored with the fence in one transaction", proc.returncode == 0,
                {"exit": proc.returncode, "stderr": proc.stderr.strip()[-300:]})
        if proc.returncode != 0:
            return
        fences = self.stack.scalar("SELECT count(*) FROM team_access_fences WHERE cleared_at IS NULL",
                                   database=RESTORE_DB)
        api = self.stack.start_api(RESTORE_DB, self.args.api_port + 1, "restore")
        try:
            _, health = api.call("GET", "/health")
            keys = {name: self.members[name].key for name in ("owner", "dev_core", "revokee", "viewer")}
            keys["oauth_dev"] = self.members["oauth_dev"].key
            fenced = {name: api.status("GET", "/decisions", key) for name, key in keys.items()}
            r.check(s, "fenced restore: no restored credential honoured (incl. the member revoked after the snapshot)",
                    fences == 1 and health.get("team_access") == "pending_reconciliation"
                    and all(v in {401, 403} for v in fenced.values()),
                    {"fences": fences, "team_access": health.get("team_access"), "statuses": fenced})
            if fenced.get("revokee") not in {401, 403}:
                self.violations.add("restore_resurrects_revoked_member", fenced)
            review = self.stack.cli_json("reconcile", database=RESTORE_DB)
            self.stack.cli_json("reconcile", "--attest", review["digest"], database=RESTORE_DB)
            _, health = api.call("GET", "/health")
            after = {name: api.status("GET", "/decisions", key) for name, key in keys.items()}
            r.check(s, "reconcile --attest (default) clears the fence and revokes every restored credential",
                    health.get("team_access") == "open" and all(v == 401 for v in after.values()),
                    {"team_access": health.get("team_access"), "statuses": after,
                     "members_reviewed": len(review.get("members", []))})
        finally:
            api.close()
            self.stack.drop(RESTORE_DB)

    # ------------------------------------------------------------------ orchestration

    def execute(self) -> None:
        self.phase_setup()
        self.results["row_counts_before"] = self.row_counts()
        load_users = [m for m in self.members.values() if m.name != "revokee"]
        self.results["load"] = self.phase_load("load", load_users, self.args.ops)
        self.phase_cas()
        self.phase_governance()
        self.phase_consolidation()
        self.phase_freshness()
        self.phase_leases()
        self.phase_provenance()
        self.snapshot_dump()
        self.phase_revocation()
        self.phase_restore_fence()
        # A last read sweep after revocation: everyone else still reads, nothing leaks.
        survivors = [m for m in self.members.values() if m.name != "revokee"]
        self.results["post_revoke_reads"] = self.phase_load("post-revoke reads", survivors, 3)
        self.results["row_counts_after"] = self.row_counts()

    def summary(self) -> dict[str, Any]:
        report = self.report.as_dict()
        report.pop("legacy_commit", None)
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source": {"commit": self.source_commit, "dirty": self.source_dirty},
            "runtime": {"platform": sys.platform, "python": platform.python_version()},
            "config": {k: v for k, v in vars(self.args).items() if k not in {"database_url"}},
            "ok": report["ok"] and not self.violations.counts,
            "checks": report,
            "violations": self.violations.as_dict(),
            "metrics": {"endpoints": latency_summary(self.metrics.samples, self.metrics.statuses),
                        "status_distribution": self.metrics.status_distribution(),
                        "phases": self.metrics.phases, "total_requests": self.metrics.total()},
            "freshness_states": {p: dict(c) for p, c in self.freshness_states.items()},
            "results": self.results,
            "not_exercised": self.not_exercised,
        }


def verify_chain_rules(rows: list[dict], canonical_id: str) -> list[str]:
    """``verify_chain`` for rules, whose ids are strings; history ids differ from the rule key."""
    mapping = {row["id"]: index + 1 for index, row in enumerate(rows)}
    converted = [{**row, "id": mapping[row["id"]],
                  "supersedes_id": mapping.get(row.get("supersedes_id")) if row.get("supersedes_id") else None,
                  "superseded_by_id": mapping.get(row.get("superseded_by_id"))
                  if row.get("superseded_by_id") else None} for row in rows]
    if canonical_id not in mapping:
        return [f"canonical rule {canonical_id} missing"]
    return verify_chain(converted, mapping[canonical_id])


def _origin_behind(envelope: dict) -> Optional[int]:
    for key in ("commits_behind_origin",):
        if envelope.get(key) is not None:
            return int(envelope[key])
    for nested in ("repository", "repo", "details", "source"):
        value = envelope.get(nested)
        if isinstance(value, dict) and value.get("commits_behind_origin") is not None:
            return int(value["commits_behind_origin"])
    return None


def _json(value: Any) -> list:
    """asyncpg returns json columns as text."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return value if isinstance(value, list) else []


def _brief(episodes: list[dict]) -> list[dict]:
    return [{"id": e["id"], "status": e["status"], "author": e["author_principal_id"],
             "gate": _json(e.get("gate_reasons"))[-2:]} for e in episodes]


class _pool:
    """ThreadPoolExecutor context with a floor of one worker."""

    def __init__(self, workers: int) -> None:
        from concurrent.futures import ThreadPoolExecutor

        self.executor = ThreadPoolExecutor(max_workers=max(1, workers))

    def __enter__(self) -> Any:
        return self.executor

    def __exit__(self, *exc: Any) -> None:
        self.executor.shutdown(wait=True)


# --------------------------------------------------------------------------- report

def render_markdown(summary: dict[str, Any]) -> str:
    lines = ["# Team memory multi-user E2E", "",
             f"Generated {summary['generated_at']} - users {summary['config']['users']}, ops/user "
             f"{summary['config']['ops']}, API workers {summary['config']['api_workers']}.", "",
             f"**Result:** {'PASS' if summary['ok'] else 'FAIL'} - {summary['checks']['passed']} checks passed, "
             f"{summary['checks']['failed']} failed, {summary['checks']['known_gaps']} known gaps, "
             f"{sum(v['count'] for v in summary['violations'].values())} invariant violation(s), "
             f"{len(summary['checks']['errors'])} error(s).", "", "## Checks", "",
             "| Status | Scenario | Check |", "|---|---|---|"]
    for check in summary["checks"]["checks"]:
        lines.append(f"| {check['status']} | {check['scenario']} | {check['name']} |")
    lines += ["", "## Invariant violations", ""]
    if not summary["violations"]:
        lines.append("None.")
    for category, data in summary["violations"].items():
        lines.append(f"- **{category}**: {data['count']}")
        for example in data["examples"][:2]:
            lines.append(f"  - `{example[:300]}`")
    lines += ["", "## Throughput", "", "| Phase | Requests | Seconds | req/s |", "|---|---|---|---|"]
    for phase, data in summary["metrics"]["phases"].items():
        lines.append(f"| {phase} | {data['requests']} | {data['seconds']} | {data['throughput_rps']} |")
    lines += ["", f"Status distribution: `{json.dumps(summary['metrics']['status_distribution'])}`", "",
              "Status `0` is a transport failure; `1` is an MCP tool result with `isError` (HTTP 200).", "",
              "## Latency by endpoint", "", "| Endpoint | n | p50 ms | p95 ms | p99 ms | max ms | statuses |",
              "|---|---|---|---|---|---|---|"]
    for label, data in sorted(summary["metrics"]["endpoints"].items(), key=lambda kv: -kv[1]["count"]):
        lines.append(f"| {label} | {data['count']} | {data['p50_ms']} | {data['p95_ms']} | {data['p99_ms']} | "
                     f"{data['max_ms']} | {json.dumps(data['statuses'])} |")
    before, after = summary["results"].get("row_counts_before", {}), summary["results"].get("row_counts_after", {})
    lines += ["", "## Rows per tier (before -> after)", "", "| Table | Before | After |", "|---|---|---|"]
    for table in sorted(set(before) | set(after)):
        lines.append(f"| {table} | `{json.dumps(before.get(table))}` | `{json.dumps(after.get(table))}` |")
    lines += ["", "## Freshness states seen", "", f"`{json.dumps(summary['freshness_states'])}`", ""]
    if summary["not_exercised"]:
        lines += ["## Not exercised", ""] + [f"- {item}" for item in summary["not_exercised"]] + [""]
    if summary["checks"]["errors"]:
        lines += ["## Errors", ""] + [f"```\n{e[-1500:]}\n```" for e in summary["checks"]["errors"]]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- CLI

def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--database-url", default=os.getenv("TEAM_MEMORY_E2E_DATABASE_URL"),
                        help="Superuser URL of a disposable PostgreSQL+pgvector server "
                             "(postgresql://user:pass@host:port/postgres); only brain_e2e_memload* databases are used")
    parser.add_argument("--redis-url", default=os.getenv("TEAM_MEMORY_E2E_REDIS_URL", "redis://127.0.0.1:6390/0"))
    parser.add_argument("--users", type=int, default=12, help="Principals (minimum 12: the base roster)")
    parser.add_argument("--ops", type=int, default=50, help="Operations per user in the concurrent load phase")
    parser.add_argument("--api-port", type=int, default=18571, help="Main API port (the next one serves the restore)")
    parser.add_argument("--api-workers", type=int, default=1, help="uvicorn worker processes for the main API")
    parser.add_argument("--cas-rounds", type=int, default=5)
    parser.add_argument("--cas-writers", type=int, default=6)
    parser.add_argument("--lease-contenders", type=int, default=8)
    parser.add_argument("--lease-churn", type=int, default=15, help="Claim/release cycles per contender")
    parser.add_argument("--revoke-threads", type=int, default=4)
    parser.add_argument("--revoke-after", type=float, default=1.5, help="Seconds of revokee traffic before revoking")
    parser.add_argument("--revoke-tail", type=float, default=2.0, help="Seconds of revokee traffic after revoking")
    parser.add_argument("--worker-timeout", type=int, default=120)
    parser.add_argument("--docker-cmd", default=os.getenv("TEAM_MEMORY_E2E_DOCKER", ""),
                        help='How to run docker, e.g. "docker" or "wsl -d Ubuntu-24.04 docker" (restore fence)')
    parser.add_argument("--pg-container", default=os.getenv("TEAM_MEMORY_E2E_PG_CONTAINER", ""),
                        help="Name of the PostgreSQL container (pg_dump/psql run inside it)")
    parser.add_argument("--seed", type=int, default=20261018)
    parser.add_argument("--out", help="Output directory (default artifacts/team-memory-e2e/<UTC timestamp>)")
    parser.add_argument("--keep-db", action="store_true", help="Leave brain_e2e_memload for inspection")
    args = parser.parse_args(argv)
    if not args.database_url:
        parser.error("--database-url (or TEAM_MEMORY_E2E_DATABASE_URL) is required")
    if args.users < len(BASE_ROSTER):
        parser.error(f"--users must be at least {len(BASE_ROSTER)} (the base roster)")
    if args.api_workers < 1:
        parser.error("--api-workers must be positive")
    if sys.platform == "win32" and args.api_workers > 1:
        parser.error("native Windows multi-worker uvicorn can block in shared-listener accept(); "
                     "run the multi-worker harness in Linux/WSL instead. "
                     "--api-workers 1 is supported but does not validate multiple workers")
    try:
        base.e2e_database_url(args.database_url, MAIN_DB)
    except ValueError as exc:
        parser.error(str(exc))
    return args


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    work = Path(args.out) if args.out else ROOT / "artifacts" / "team-memory-e2e" / stamp
    work.mkdir(parents=True, exist_ok=True)
    run = Run(args, work)
    try:
        run.execute()
    except Exception as exc:  # noqa: BLE001 - recorded; the report says where it stopped
        run.report.errors.append(f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-3000:]}")
        print(base.redact(run.report.errors[-1], run.report.secrets), file=sys.stderr)
    finally:
        if run.api is not None:
            run.api.close()
        run.stack.stop()
        if not args.keep_db:
            run.stack.drop(MAIN_DB)
        run.stack.drop(RESTORE_DB)
        summary = run.summary()
        text = base.redact(json.dumps(summary, indent=2, default=str), run.report.secrets)
        (work / "report.json").write_text(text, encoding="utf-8")
        (work / "REPORT.md").write_text(base.redact(render_markdown(json.loads(text)), run.report.secrets),
                                        encoding="utf-8")
        for name in ("snapshot.sql", "restore-input.sql"):
            (work / name).unlink(missing_ok=True)  # contain credential hashes; not needed after the run
        print(f"\nteam memory e2e: {'PASS' if summary['ok'] else 'FAIL'} - {summary['checks']['passed']} passed, "
              f"{summary['checks']['failed']} failed, {summary['checks']['known_gaps']} known gaps, "
              f"{sum(run.violations.counts.values())} violations; report: {work / 'REPORT.md'}")
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
