#!/usr/bin/env python3
"""Team-mode end-to-end check, including the upgrade from the release prod runs.

Repeatable, opt-in check (not part of the normal test suite). It needs a
disposable PostgreSQL server (with pgvector), a Redis instance, ``pg_dump`` and
``psql`` on PATH, and an extracted tree of the legacy release
(``git archive c13d056 | tar -x -C <dir>``). Every database it touches is
named ``brain_e2e_*``; it creates and drops only those. Neo4j is not needed:
the API and the backup run without it (``--allow-missing neo4j``).

Scenarios (each check prints PASS/FAIL and lands in the JSON report):

1. Legacy upgrade: c13d056 schema + representative rows, then main's startup
   migrations; legacy rows preserved, team_state solo, owner key works,
   decision edit keeps the id, bumps the version and writes a history row.
2. Repeat initialization is idempotent (schema init and ``brain team`` init).
3. Team path over HTTP against a running API: invitations, member sign-in,
   allowed/denied reads, memory writes, OAuth, runs, revocation, dissolve.
4. Backup -> ``scripts/restore_database.py`` -> fenced -> ``brain team
   reconcile --attest`` -> credentials work again; plain psql restore is not
   fenced.
5. Freshness envelope in API and MCP replies.

Run locally with ``scripts/team_e2e_docker.sh`` (containers) or directly::

    python scripts/team_e2e_check.py --database-url postgresql://u:p@127.0.0.1:5448/postgres \\
        --redis-url redis://127.0.0.1:6398/0 --legacy-tree /tmp/legacy-c13d056

See docs/runbooks/team-e2e-check.md.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
DB_PREFIX = "brain_e2e_"
LEGACY_COMMIT = "c13d056"
MAIN_DB = f"{DB_PREFIX}main"
RESTORE_KEEP_DB = f"{DB_PREFIX}restore_keep"
RESTORE_REVOKE_DB = f"{DB_PREFIX}restore_revoke"
PLAIN_DB = f"{DB_PREFIX}plain"
REPO_A = "/e2e/alpha"
REPO_B = "/e2e/beta"
JOB_PREFIX = "brain-e2e:worker"
SECRET_LABEL = "(shown once, never stored in plaintext):"


# --------------------------------------------------------------------------- safety guards

def e2e_database_url(admin_url: str, database: str) -> str:
    """``admin_url`` pointed at ``database``; refuses any name outside the ``brain_e2e_`` namespace."""
    if not database.startswith(DB_PREFIX) or not database.replace("_", "").isalnum():
        raise ValueError(f"Refusing database {database!r}: the e2e check only touches {DB_PREFIX}* databases")
    parts = urlsplit(admin_url)
    if parts.scheme not in {"postgresql", "postgres"}:
        raise ValueError("--database-url must be a postgresql:// URL")
    if parts.query or parts.fragment:
        raise ValueError("--database-url must not carry query parameters")
    return urlunsplit(("postgresql", parts.netloc, f"/{database}", "", ""))


def brain_url(url: str) -> str:
    """The SQLAlchemy asyncpg form Brain's settings take."""
    return "postgresql+asyncpg://" + url.split("://", 1)[1]


def redact(text: str, secrets_seen: list[str]) -> str:
    for value in secrets_seen:
        if value:
            text = text.replace(value, "<redacted>")
    return text


def shown_secret(stderr: str) -> str:
    """The value the CLI printed after its 'shown once' label (stderr)."""
    lines = [line.strip() for line in stderr.splitlines()]
    for index, line in enumerate(lines):
        if line.endswith(SECRET_LABEL) and index + 1 < len(lines) and lines[index + 1]:
            return lines[index + 1]
    raise AssertionError("CLI did not print a secret after its label")


# --------------------------------------------------------------------------- report

@dataclass
class Check:
    scenario: str
    name: str
    status: str  # PASS, FAIL, GAP (known, documented gap still present) or GAP-FIXED (it no longer reproduces)
    evidence: str

    @property
    def ok(self) -> bool:
        return self.status != "FAIL"


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    secrets: list[str] = field(default_factory=list)

    def check(self, scenario: str, name: str, ok: Any, evidence: Any = "") -> bool:
        return self._record(scenario, name, "PASS" if ok else "FAIL", evidence) == "PASS"

    def gap(self, scenario: str, name: str, ok: Any, evidence: Any = "") -> bool:
        """A known gap (reported, not fixed here): ``ok`` is the desired behaviour. While the
        gap reproduces the check is GAP and does not fail the run; GAP-FIXED means it was
        fixed upstream and the check should become a plain ``check``."""
        return self._record(scenario, name, "GAP-FIXED" if ok else "GAP", evidence) == "GAP-FIXED"

    def _record(self, scenario: str, name: str, status: str, evidence: Any) -> str:
        text = evidence if isinstance(evidence, str) else json.dumps(evidence, default=str, sort_keys=True)
        text = redact(text, self.secrets)[:600]
        self.checks.append(Check(scenario, name, status, text))
        print(f"[{status}] {scenario} {name} -- {text}", flush=True)
        return status

    def secret(self, value: str) -> str:
        self.secrets.append(value)
        return value

    def count(self, status: str) -> int:
        return sum(c.status == status for c in self.checks)

    @property
    def failed(self) -> bool:
        return bool(self.errors) or any(not c.ok for c in self.checks)

    def as_dict(self) -> dict:
        return {"ok": not self.failed, "legacy_commit": LEGACY_COMMIT,
                "passed": self.count("PASS"), "failed": self.count("FAIL"),
                "known_gaps": self.count("GAP"), "gaps_fixed": self.count("GAP-FIXED"),
                "errors": [redact(e, self.secrets) for e in self.errors],
                "checks": [vars(c) for c in self.checks]}


# --------------------------------------------------------------------------- environment

class Env:
    def __init__(self, args: argparse.Namespace, report: Report) -> None:
        self.args = args
        self.report = report
        self.work = Path(args.work_dir).resolve()
        self.work.mkdir(parents=True, exist_ok=True)
        self.legacy = Path(args.legacy_tree).resolve()
        self.shared_key = report.secret(f"e2e-shared-{secrets.token_urlsafe(24)}")
        self.pg_password = secrets.token_urlsafe(16)
        self.neo4j_password = secrets.token_urlsafe(16)
        self.webhook = secrets.token_urlsafe(32)
        self.processes: list[subprocess.Popen] = []

    def url(self, database: str) -> str:
        return e2e_database_url(self.args.database_url, database)

    def brain_env(self, database: str, **extra: str) -> dict[str, str]:
        """Production-like settings for main's code (prod pins from the c13d056 -> main checklist)."""
        keep = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "SYSTEMROOT", "TEMP", "TMP", "LANG"}}
        env = {
            **keep,
            "PYTHONPATH": str(ROOT),
            "PYTHONUNBUFFERED": "1",
            "ENVIRONMENT": "production",
            "BRAIN_STORAGE_MODE": "full",
            "DATABASE_URL": brain_url(self.url(database)),
            "POSTGRES_PASSWORD": self.pg_password,
            "REDIS_URL": self.args.redis_url,
            "WORKER_REDIS_PREFIX": JOB_PREFIX,
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
        }
        env.update(extra)
        return env

    # -- subprocess helpers -------------------------------------------------------------

    def run(self, argv: list[str], *, env: dict[str, str], cwd: Path, timeout: int = 300,
            check: bool = True) -> subprocess.CompletedProcess:
        proc = subprocess.run(argv, env=env, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
        if check and proc.returncode != 0:
            raise RuntimeError(f"{' '.join(argv[:4])}... exited {proc.returncode}:\n"
                               f"{redact(proc.stdout[-2000:], self.report.secrets)}\n"
                               f"{redact(proc.stderr[-4000:], self.report.secrets)}")
        return proc

    def main_py(self, code: str, database: str, **extra: str) -> Any:
        """Run ``code`` with main's tree; it must print one JSON document as its last stdout line."""
        proc = self.run([sys.executable, "-c", code], env=self.brain_env(database, **extra), cwd=ROOT)
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def legacy_py(self, code: str, database: str) -> Any:
        env = self.brain_env(database)
        env["PYTHONPATH"] = str(self.legacy)
        env["PROJECT_BRAIN_GIT_REPO_PATH"] = str(self.legacy)
        proc = self.run([sys.executable, "-c", code], env=env, cwd=self.legacy)
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def cli(self, database: str, *argv: str, check: bool = True, **extra: str) -> subprocess.CompletedProcess:
        return self.run([sys.executable, "-m", "apps.cli.main", "team", *argv],
                        env=self.brain_env(database, **extra), cwd=ROOT, check=check)

    def cli_json(self, database: str, *argv: str, **extra: str) -> Any:
        return json.loads(self.cli(database, *argv, check=True, **extra).stdout)

    def psql(self, url: str, *argv: str, check: bool = True) -> subprocess.CompletedProcess:
        parts = urlsplit(url)
        env = {**os.environ, "PGPASSWORD": parts.password or ""}
        safe = urlunsplit((parts.scheme, f"{parts.username}@{parts.hostname}:{parts.port or 5432}", parts.path, "", ""))
        return self.run(["psql", "--dbname", safe, "-X", "-v", "ON_ERROR_STOP=1", *argv], env=env, cwd=self.work,
                        check=check)

    def sql(self, database: str, query: str) -> list[dict]:
        """Rows as dicts (one JSON object per row, through psql)."""
        wrapped = f"SELECT coalesce(json_agg(t), '[]'::json) FROM ({query}) t"
        out = self.psql(self.url(database), "-t", "-A", "-c", wrapped).stdout.strip()
        return json.loads(out or "[]")

    def recreate(self, database: str) -> None:
        admin = self.url(f"{DB_PREFIX}admin").rsplit("/", 1)[0] + "/postgres"
        self.url(database)  # guard
        self.psql(admin, "-c", f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='{database}' "
                               "AND pid<>pg_backend_pid()")
        self.psql(admin, "-c", f'DROP DATABASE IF EXISTS "{database}"')
        self.psql(admin, "-c", f'CREATE DATABASE "{database}"')

    def drop(self, database: str) -> None:
        admin = self.url(f"{DB_PREFIX}admin").rsplit("/", 1)[0] + "/postgres"
        self.url(database)
        self.psql(admin, "-c", f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='{database}' "
                               "AND pid<>pg_backend_pid()", check=False)
        self.psql(admin, "-c", f'DROP DATABASE IF EXISTS "{database}"', check=False)

    def flush_redis(self) -> None:
        self.main_py("import asyncio, json, redis.asyncio as r, os\n"
                     "async def m():\n"
                     "    c = r.from_url(os.environ['REDIS_URL'])\n"
                     "    keys = [k async for k in c.scan_iter(match='brain-e2e:*')]\n"
                     "    if keys: await c.delete(*keys)\n"
                     "    await c.aclose()\n"
                     "    print(json.dumps(len(keys)))\n"
                     "asyncio.run(m())", MAIN_DB)

    # -- API --------------------------------------------------------------------------

    def start_api(self, database: str, port: int, label: str) -> "Api":
        log = open(self.work / f"api-{label}.log", "w", encoding="utf-8")
        base = f"http://127.0.0.1:{port}"
        env = self.brain_env(database, BRAIN_PUBLIC_URL=base)
        proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "apps.api.main:app", "--host", "127.0.0.1",
                                 "--port", str(port), "--log-level", "warning"],
                                env=env, cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT)
        self.processes.append(proc)
        api = Api(base, proc, self.report)
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

    def stop(self) -> None:
        for proc in self.processes:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    proc.kill()
        self.processes.clear()


class Api:
    def __init__(self, base: str, proc: subprocess.Popen, report: Report) -> None:
        import httpx

        self.base = base
        self.proc = proc
        self.report = report
        self.http = httpx.Client(base_url=base, timeout=60.0, follow_redirects=False)

    def call(self, method: str, path: str, key: Optional[str] = None, **kwargs: Any) -> tuple[int, Any]:
        headers = dict(kwargs.pop("headers", {}) or {})
        if key:
            headers["X-API-Key"] = key
        response = self.http.request(method, path, headers=headers, **kwargs)
        try:
            body: Any = response.json()
        except ValueError:
            body = response.text[:300]
        return response.status_code, body

    def status(self, method: str, path: str, key: Optional[str] = None, **kwargs: Any) -> int:
        return self.call(method, path, key, **kwargs)[0]

    def close(self) -> None:
        self.http.close()
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def detail(body: Any) -> str:
    return str(body.get("detail")) if isinstance(body, dict) else str(body)[:200]


# --------------------------------------------------------------------------- legacy seed

LEGACY_SEED = r"""
import asyncio, json, os
from datetime import timedelta

async def main():
    from brain.database.session import init_db, async_session_factory
    await init_db()
    from brain.auth.principals import mint_credential
    from brain.auth import oauth_store
    from brain.database.models import Decision, Learning, Repository, Rule, WorkerJobLease
    from brain.database.harness_models import AgentTask
    from brain.workers.queue import JobQueue
    from datetime import date, datetime, timezone
    import redis.asyncio as aioredis
    out = {}
    async with async_session_factory() as s:
        s.add_all([
            Repository(name="alpha", path="/e2e/alpha", indexing_status="completed", last_indexed_commit="a" * 40),
            Repository(name="beta", path="/e2e/beta", indexing_status="completed", last_indexed_commit="b" * 40),
        ])
        s.add_all([
            Decision(title="Use pgvector", repo_path="/e2e/alpha", description="Legacy decision v1",
                     status="active", date=date(2026, 7, 1), reason="One store", affected_files=["db.py"]),
            Decision(title="Adopt FastAPI", repo_path="/e2e/beta", description="Beta-only decision", status="active"),
            # Legacy uniqueness is (lower(title), lower(coalesce(repo_path,''))): NULL and
            # 'system_global' are distinct there, one scope on main -> documented supersede.
            Decision(title="Global policy", repo_path=None, description="Older global row", status="active"),
            Decision(title="Global Policy", repo_path="system_global", description="Newer global row",
                     status="active"),
        ])
        s.add_all([
            Rule(id="legacy-rule-alpha", name="Pin dependencies", repo_path="/e2e/alpha",
                 description="Legacy rule", type="architecture", severity="high", status="active"),
            Rule(id="legacy-rule-global", name="No secrets in git", repo_path="system_global",
                 description="Legacy global rule", type="security", severity="critical", status="active"),
        ])
        s.add(Learning(statement="Legacy learning about alpha", category="ops", confidence=0.8,
                       evidence=[{"source": "legacy"}], status="active", repo_scope="/e2e/alpha"))
        s.add(AgentTask(title="Legacy harness task", goal="Keep working", repo_path="/e2e/alpha"))
        s.add(WorkerJobLease(job_id="legacy-job-lease", fencing_token="f" * 16, worker_id="legacy-worker",
                             expires_at=datetime(2026, 7, 1, tzinfo=timezone.utc)))
        owner_key, _ = await mint_credential(s, name="owner", scopes=["*"], kind="human")
        ci_key, _ = await mint_credential(s, name="ci-indexer", scopes=["jobs:write", "jobs:read"], kind="service")
        await oauth_store.save_client(s, "legacy-client-0001", {"client_id": "legacy-client-0001",
                                                                "client_name": "legacy connector",
                                                                "redirect_uris": ["http://127.0.0.1:9/cb"]})
        issued = await oauth_store.issue_tokens(s, client_id="legacy-client-0001", client_name="legacy connector",
                                                scopes=["core:write"], subject="owner",
                                                access_ttl=timedelta(hours=1), refresh_ttl=timedelta(days=30))
        await s.commit()
    redis = aioredis.from_url(os.environ["REDIS_URL"])
    job_id = await JobQueue(redis, os.environ["WORKER_REDIS_PREFIX"]).enqueue(
        "index_repository", {"repo_path": "/e2e/alpha"})
    await redis.aclose()
    print(json.dumps({"owner_key": owner_key, "ci_key": ci_key, "oauth_access": issued.access_token,
                      "oauth_refresh": issued.refresh_token, "job_id": job_id}))

asyncio.run(main())
"""

INIT_DB = ("import asyncio, json\n"
           "from brain.database.session import init_db\n"
           "asyncio.run(init_db())\n"
           "print(json.dumps('ok'))")

TEAM_STATE = ("import asyncio, json\n"
              "from brain.database.session import async_session_factory\n"
              "from brain.team.authorization import team_state\n"
              "async def m():\n"
              "    async with async_session_factory() as s:\n"
              "        return await team_state(s)\n"
              "print(json.dumps(asyncio.run(m())))")

JOB_REFUSAL = ("import asyncio, json, os, sys\n"
               "import redis.asyncio as aioredis\n"
               "from brain.workers.queue import JobQueue\n"
               "from brain.workers.initiator import initiator_refusal\n"
               "async def m():\n"
               "    r = aioredis.from_url(os.environ['REDIS_URL'])\n"
               "    job = await JobQueue(r, os.environ['WORKER_REDIS_PREFIX']).get_job(os.environ['E2E_JOB_ID'])\n"
               "    await r.aclose()\n"
               "    return {'found': job is not None, 'refusal': await initiator_refusal(job) if job else None}\n"
               "print(json.dumps(asyncio.run(m())))")

SNAPSHOT_TABLES = {
    "repositories": "id",
    "decisions": "id",
    "rules": "id",
    "memory_learnings": "id",
    "principals": "id",
    "api_credentials": "id",
    "oauth_clients": "client_id",
    "oauth_refresh_tokens": "id",
    "harness.agent_tasks": "id",
    "worker_job_leases": "job_id",
}


def snapshot(env: Env, database: str) -> dict[str, dict[str, dict]]:
    result = {}
    for table, key in SNAPSHOT_TABLES.items():
        rows = env.sql(database, f"SELECT * FROM {table}")
        result[table] = {str(row[key]): row for row in rows}
    return result


def legacy_differences(before: dict, after: dict, allowed: Callable[[str, str, str], bool]) -> list[str]:
    """Legacy columns whose values changed (new columns are ignored); ``allowed(table, pk, column)`` exempts."""
    problems = []
    for table, rows in before.items():
        for pk, row in rows.items():
            new = after.get(table, {}).get(pk)
            if new is None:
                problems.append(f"{table}[{pk}] missing")
                continue
            for column, value in row.items():
                if new.get(column) != value and not allowed(table, pk, column):
                    problems.append(f"{table}[{pk}].{column}: {value!r} -> {new.get(column)!r}")
    return problems


# --------------------------------------------------------------------------- scenarios

@dataclass
class State:
    owner_key: str = ""
    ci_key: str = ""
    legacy_oauth_access: str = ""
    legacy_oauth_refresh: str = ""
    job_id: str = ""
    repo_a: int = 0
    repo_b: int = 0
    pgvector_decision: int = 0
    admin_key: str = ""
    admin_id: int = 0
    alice_key: str = ""
    alice_id: int = 0
    alice_invitation: str = ""
    bob_key: str = ""
    bob_id: int = 0
    bob_access: str = ""
    bob_refresh: str = ""
    alice_oauth_access: str = ""
    alice_oauth_refresh: str = ""
    alice_oauth_client: str = ""
    oauth_client: str = ""
    project_core: str = ""
    project_other: str = ""
    run_key: str = ""
    task_id: str = ""
    backup_dir: Optional[Path] = None


def scenario_1_legacy_upgrade(env: Env, st: State) -> None:
    r = env.report
    s = "1-upgrade"
    env.recreate(MAIN_DB)
    env.flush_redis()
    seeded = env.legacy_py(LEGACY_SEED, MAIN_DB)
    st.owner_key = r.secret(seeded["owner_key"])
    st.ci_key = r.secret(seeded["ci_key"])
    st.legacy_oauth_access = r.secret(seeded["oauth_access"])
    st.legacy_oauth_refresh = r.secret(seeded["oauth_refresh"])
    st.job_id = seeded["job_id"]
    legacy_tables = {row["table_name"] for row in env.sql(
        MAIN_DB, "SELECT table_name FROM information_schema.tables WHERE table_schema='public'")}
    r.check(s, "legacy schema built by c13d056 init_db", "teams" not in legacy_tables and "decisions" in legacy_tables,
            f"{len(legacy_tables)} public tables, no team tables")
    before = snapshot(env, MAIN_DB)
    counts = {table: len(rows) for table, rows in before.items()}
    r.check(s, "legacy rows inserted", all(counts[t] > 0 for t in counts), counts)

    env.main_py(INIT_DB, MAIN_DB)
    after = snapshot(env, MAIN_DB)
    decisions = {row["title"]: row for row in after["decisions"].values()}
    older = next(row for row in after["decisions"].values() if row["title"] == "Global policy")
    newer = next(row for row in after["decisions"].values() if row["title"] == "Global Policy")

    def allowed(table: str, pk: str, column: str) -> bool:
        # The documented startup supersede of the older duplicate global decision.
        return table == "decisions" and pk == str(older["id"]) and column == "status"

    problems = legacy_differences(before, after, allowed)
    r.check(s, "legacy rows preserved (every legacy column unchanged)", not problems, problems or "no differences")
    r.check(s, "duplicate global decision superseded as documented (older -> newer, ids kept)",
            older["status"] == "superseded" and older["superseded_by_id"] == newer["id"]
            and newer["superseded_by_id"] is None,
            {"older": [older["id"], older["status"], older["superseded_by_id"]], "newer": newer["id"]})
    legacy_decisions = [row for row in after["decisions"].values()]
    r.check(s, "no reinterpretation: legacy decisions keep NULL authorship and version 1",
            all(row.get("author_principal_id") is None and row.get("updated_by_principal_id") is None
                and row.get("version") == 1 for row in legacy_decisions),
            [[row["id"], row.get("author_principal_id"), row.get("version")] for row in legacy_decisions])
    rules = list(after["rules"].values())
    learnings = list(after["memory_learnings"].values())
    r.check(s, "no reinterpretation: legacy rules/learnings keep NULL authorship",
            all(row.get("author_principal_id") is None for row in rules + learnings)
            and all(row.get("version") == 1 for row in rules), {"rules": len(rules), "learnings": len(learnings)})
    state = env.main_py(TEAM_STATE, MAIN_DB)
    r.check(s, "team_state() == solo after upgrade", state == "solo", state)
    st.repo_a = next(int(k) for k, v in after["repositories"].items() if v["path"] == REPO_A)
    st.repo_b = next(int(k) for k, v in after["repositories"].items() if v["path"] == REPO_B)
    st.pgvector_decision = decisions["Use pgvector"]["id"]
    refusal = env.main_py(JOB_REFUSAL, MAIN_DB, E2E_JOB_ID=st.job_id)
    r.check(s, "legacy queued job (no initiator) still runnable in solo", refusal == {"found": True, "refusal": None},
            refusal)


def scenario_1_http(env: Env, api: Api, st: State) -> None:
    r = env.report
    s = "1-upgrade"
    status, health = api.call("GET", "/health")
    r.check(s, "/health mode solo, team_access open",
            isinstance(health, dict) and health.get("mode") == "solo" and health.get("team_access") == "open",
            {"status": status, "mode": health.get("mode"), "team_access": health.get("team_access")})
    status, body = api.call("GET", "/decisions", st.owner_key)
    titles = {row.get("title") for row in body} if isinstance(body, list) else set()
    r.check(s, "legacy owner pbk key works (GET /decisions)", status == 200 and "Use pgvector" in titles,
            {"status": status, "decisions": len(titles)})
    r.check(s, "legacy shared key works in solo", api.status("GET", "/decisions", env.shared_key) == 200)
    r.check(s, "legacy OAuth access token works in solo", api.status("GET", "/decisions", st.legacy_oauth_access) == 200)
    r.check(s, "no credential is refused (production)", api.status("GET", "/decisions") == 401)

    before = env.sql(MAIN_DB, f"SELECT * FROM decisions WHERE id={st.pgvector_decision}")[0]
    status, body = api.call("POST", "/decisions", st.owner_key, json={
        "title": "Use pgvector", "repo_path": REPO_A, "description": "Edited after upgrade", "expected_version": 1})
    r.check(s, "decision edit keeps the id and bumps the version",
            status == 200 and body.get("decision_id") == st.pgvector_decision and body.get("version") == 2
            and body.get("changed") is True, body)
    canonical = env.sql(MAIN_DB, f"SELECT * FROM decisions WHERE id={st.pgvector_decision}")[0]
    history = env.sql(MAIN_DB, f"SELECT * FROM decisions WHERE superseded_by_id={st.pgvector_decision}")
    ok = (len(history) == 1 and history[0]["status"] == "superseded"
          and history[0]["description"] == before["description"] and history[0]["version"] == 1
          and canonical["supersedes_id"] == history[0]["id"] and canonical["description"] == "Edited after upgrade")
    r.check(s, "edit writes a superseded history row with the legacy content", ok,
            {"history": [(h["id"], h["status"], h["version"]) for h in history],
             "supersedes_id": canonical["supersedes_id"]})
    owner_id = env.sql(MAIN_DB, "SELECT id FROM principals WHERE name='owner'")[0]["id"]
    r.check(s, "edit records the editor as updated_by", canonical["updated_by_principal_id"] == owner_id,
            {"updated_by": canonical["updated_by_principal_id"], "owner": owner_id})
    r.check(s, "legacy authorship stays unknown after an edit (author NULL on canonical and history)",
            canonical["author_principal_id"] is None and all(h["author_principal_id"] is None for h in history),
            {"canonical_author": canonical["author_principal_id"],
             "history_author": [h["author_principal_id"] for h in history]})
    status, body = api.call("POST", "/decisions", st.owner_key, json={
        "title": "Use pgvector", "repo_path": REPO_A, "description": "Stale write", "expected_version": 1})
    r.check(s, "stale expected_version is refused with 409", status == 409, {"status": status, "detail": detail(body)})


def scenario_2_idempotent(env: Env, st: State) -> None:
    r = env.report
    s = "2-idempotent"
    before = snapshot(env, MAIN_DB)
    columns = env.sql(MAIN_DB, "SELECT table_schema, table_name, column_name FROM information_schema.columns "
                               "WHERE table_schema IN ('public','harness') ORDER BY 1,2,3")
    env.main_py(INIT_DB, MAIN_DB)
    after = snapshot(env, MAIN_DB)
    columns_after = env.sql(MAIN_DB, "SELECT table_schema, table_name, column_name FROM information_schema.columns "
                                     "WHERE table_schema IN ('public','harness') ORDER BY 1,2,3")
    r.check(s, "second schema init changes no row and no column", before == after and columns == columns_after,
            {"columns": len(columns)})
    first = env.cli_json(MAIN_DB, "init", "--name", "E2E Team")
    second = env.cli_json(MAIN_DB, "init", "--name", "E2E Team")
    r.check(s, "brain team init twice returns the same team", first == second, first)
    core = env.cli_json(MAIN_DB, "project", "add", "core")
    core_again = env.cli_json(MAIN_DB, "project", "add", "core")
    other = env.cli_json(MAIN_DB, "project", "add", "other")
    st.project_core, st.project_other = core["id"], other["id"]
    r.check(s, "brain team project add twice returns the same project", core["id"] == core_again["id"], core["id"])
    a1 = env.cli_json(MAIN_DB, "repo", "attach", "--project", "core", "--repository", REPO_A)
    a2 = env.cli_json(MAIN_DB, "repo", "attach", "--project", "core", "--repository", REPO_A)
    env.cli_json(MAIN_DB, "repo", "attach", "--project", "other", "--repository", REPO_B)
    r.check(s, "brain team repo attach twice is idempotent", a1 == a2, a1)
    conflict = env.cli(MAIN_DB, "repo", "attach", "--project", "other", "--repository", REPO_A, check=False)
    r.check(s, "attaching a repository to a second project is refused", conflict.returncode != 0,
            conflict.stderr.strip()[-200:])
    state = env.main_py(TEAM_STATE, MAIN_DB)
    r.check(s, "team without members is still solo", state == "solo", state)


def oauth_flow(api: Api, approver_key: str, scopes: str, client_name: str) -> dict:
    """Full PKCE authorization-code flow; the approver proves itself with an API key on the consent page."""
    status, client = api.call("POST", "/register", json={
        "redirect_uris": ["http://127.0.0.1:9/cb"], "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        "client_name": client_name})
    if status not in {200, 201}:
        return {"error": f"register {status}: {client}"}
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    response = api.http.get("/authorize", params={
        "response_type": "code", "client_id": client["client_id"], "redirect_uri": "http://127.0.0.1:9/cb",
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "e2e", "scope": scopes,
        "resource": f"{api.base}/mcp"})
    if response.status_code != 302:
        return {"error": f"authorize {response.status_code}: {response.text[:200]}"}
    request_id = parse_qs(urlsplit(response.headers["location"]).query)["request"][0]
    response = api.http.post("/oauth/consent", data={"request": request_id, "action": "allow", "api_key": approver_key})
    if response.status_code != 302:
        text = response.text
        marker = text.find('class="err"')
        return {"error": f"consent {response.status_code}", "client_id": client["client_id"],
                "page": text[marker:marker + 200] if marker >= 0 else text[:200]}
    code = parse_qs(urlsplit(response.headers["location"]).query)["code"][0]
    response = api.http.post("/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": "http://127.0.0.1:9/cb",
        "client_id": client["client_id"], "code_verifier": verifier, "resource": f"{api.base}/mcp"})
    if response.status_code != 200:
        return {"error": f"token {response.status_code}: {response.text[:200]}"}
    return {"client_id": client["client_id"], **response.json()}


def refresh_grant(api: Api, client_id: str, refresh_token: str) -> tuple[int, dict]:
    response = api.http.post("/token", data={"grant_type": "refresh_token", "refresh_token": refresh_token,
                                             "client_id": client_id})
    try:
        body = response.json()
    except ValueError:
        body = {}
    return response.status_code, body if isinstance(body, dict) else {}


def refresh(api: Api, client_id: str, refresh_token: str) -> int:
    return refresh_grant(api, client_id, refresh_token)[0]


async def _mcp_call(base: str, key: str, tool: str, arguments: dict) -> dict:
    import httpx2  # the HTTP client the mcp SDK is typed against
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {key}"}, timeout=60) as http_client:
        async with Client(streamable_http_client(f"{base}/mcp", http_client=http_client), mode="auto") as client:
            result = await client.call_tool(tool, arguments)
    text = "".join(getattr(part, "text", "") for part in result.content)
    return {"is_error": bool(result.is_error), "text": text, "structured": result.structured_content}


def mcp_call(base: str, key: str, tool: str, arguments: dict) -> dict:
    try:
        return asyncio.run(_mcp_call(base, key, tool, arguments))
    except BaseException as exc:  # noqa: BLE001 - an auth failure surfaces as an exception group
        return {"exception": f"{type(exc).__name__}: {exc}"[:300]}


def mcp_status(base: str, key: Optional[str]) -> int:
    import httpx

    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "e2e", "version": "1"}}}
    return httpx.post(f"{base}/mcp", headers=headers, json=body, timeout=30).status_code


def scenario_3_team(env: Env, api: Api, st: State) -> None:
    r = env.report
    s = "3-team"
    # Solo refuses API invitations: the first administrator comes from the CLI.
    status, body = api.call("POST", "/team/invitations", st.owner_key, json={"is_admin": True, "name": "ops"})
    r.check(s, "API refuses invitations while solo", status == 409, {"status": status, "detail": detail(body)})
    created = env.cli(MAIN_DB, "invite", "create", "--admin", "--project", "core", "--role", "reviewer",
                      "--name", "ops")
    admin_token = r.secret(shown_secret(created.stderr))
    accepted = env.cli(MAIN_DB, "invite", "accept", admin_token, "--api-url", api.base)
    st.admin_key = r.secret(shown_secret(accepted.stderr))
    st.admin_id = env.sql(MAIN_DB, "SELECT id FROM principals WHERE name='ops'")[0]["id"]
    status, health = api.call("GET", "/health")
    r.check(s, "first accepted invitation turns the Brain into team state", health.get("mode") == "team", health.get("mode"))
    again = env.cli(MAIN_DB, "invite", "accept", admin_token, "--api-url", api.base, check=False)
    r.check(s, "invitation token is single-use", again.returncode == 0 and "already accepted" in again.stderr
            or again.returncode != 0, again.stderr.strip()[-160:])

    status, body = api.call("GET", "/decisions", env.shared_key)
    r.check(s, "shared key refused in team state", status == 403 and "Named credential required" in detail(body),
            {"status": status, "detail": detail(body)})
    status, body = api.call("GET", "/decisions", st.owner_key)
    r.check(s, "legacy owner pbk (not a member) sees no repository memory",
            status in {401, 403} or (status == 200 and not any(row.get("repo_path") for row in body)),
            {"status": status, "rows": len(body) if isinstance(body, list) else detail(body)})
    r.check(s, "legacy owner pbk (not a member) refused dashboard sign-in",
            api.status("GET", "/api/web-session", st.owner_key) == 403)
    refusal = env.main_py(JOB_REFUSAL, MAIN_DB, E2E_JOB_ID=st.job_id)
    r.check(s, "legacy queued job (no initiator) refused in team state",
            bool(refusal.get("refusal")) and "before initiators were recorded" in refusal["refusal"], refusal)

    status, body = api.call("GET", "/api/web-session", st.admin_key)
    r.check(s, "admin signs in to the dashboard with team_admin", status == 200 and body.get("team_admin") is True,
            {"status": status, "team_admin": body.get("team_admin") if isinstance(body, dict) else None})

    status, body = api.call("POST", "/team/invitations", st.admin_key, json={
        "projects": ["core"], "role": "contributor", "name": "alice", "email": "alice@example.invalid"})
    ok = status == 200 and body.get("token", "").startswith("pbi_")
    r.check(s, "admin creates an invitation over the API", ok, {"status": status, "state": body.get("invitation", {})
                                                                .get("state") if isinstance(body, dict) else None})
    token = r.secret(body["token"])
    st.alice_invitation = body["invitation"]["id"]

    def invitation_state() -> str:
        _, rows = api.call("GET", "/team/invitations", st.admin_key)
        return next((row["state"] for row in rows if row["id"] == st.alice_invitation), "missing")

    r.check(s, "invitation state: invited", invitation_state() == "invited")
    status, body = api.call("POST", "/team/invitations/accept", json={"token": token})
    r.check(s, "invitee accepts without a credential and gets a member key",
            status == 200 and str(body.get("credential", "")).startswith("pbk_"),
            {"status": status, "keys": sorted(body) if isinstance(body, dict) else detail(body)})
    st.alice_key = r.secret(body["credential"])
    st.alice_id = env.sql(MAIN_DB, "SELECT id FROM principals WHERE name='alice'")[0]["id"]
    r.check(s, "invitation state: verified (accepted, key unused)", invitation_state() == "verified")

    status, body = api.call("GET", "/api/web-session", st.alice_key)
    r.check(s, "member signs in to the dashboard (#121), no team_admin",
            status == 200 and body.get("team_admin") is False and body.get("member", {}).get("verified_at"),
            {"status": status, "body": body})
    r.check(s, "invitation state: active (key used)", invitation_state() == "active")

    status, body = api.call("GET", "/decisions", st.alice_key)
    paths = {row.get("repo_path") for row in body} if isinstance(body, list) else set()
    r.check(s, "member reads allowed project memory, not the other project's",
            status == 200 and REPO_A in paths and REPO_B not in paths, {"status": status, "repo_paths": sorted(map(str, paths))})
    status, body = api.call("GET", "/repositories", st.alice_key)
    names = [row["path"] for row in body.get("repositories", [])] if isinstance(body, dict) else []
    r.check(s, "GET /repositories lists only readable repositories", status == 200 and names == [REPO_A],
            {"status": status, "paths": names})
    status, body = api.call("GET", "/learnings", st.alice_key, params={"repo_path": REPO_A})
    r.check(s, "member reads learnings of the allowed repository", status == 200, {"status": status})
    status, body = api.call("GET", "/learnings", st.alice_key, params={"repo_path": REPO_B})
    r.check(s, "member denied on the other project's repository", status == 403, {"status": status, "detail": detail(body)})
    for method, path, payload in [("GET", "/admin/principals", None), ("GET", "/team/invitations", None),
                                  ("POST", "/team/invitations", {"name": "mallory"}), ("GET", "/team/projects/", None),
                                  ("GET", "/episodes", None), ("GET", "/audit/events", None)]:
        status, body = api.call(method, path, st.alice_key, json=payload)
        r.check(s, f"member denied on admin route {method} {path}", status in {401, 403},
                {"status": status, "detail": detail(body)[:120]})

    status, body = api.call("POST", "/decisions", st.alice_key, json={
        "title": "Alice decision", "repo_path": REPO_A, "description": "Recorded by a member"})
    alice_decision = body.get("decision_id") if isinstance(body, dict) else None
    author = env.sql(MAIN_DB, f"SELECT author_principal_id FROM decisions WHERE id={int(alice_decision or 0)}")
    r.check(s, "member records a decision in the allowed project, attributed to her",
            status == 200 and author and author[0]["author_principal_id"] == st.alice_id,
            {"status": status, "author": author})
    status, body = api.call("POST", "/rules", st.alice_key, json={
        "name": "Alice rule", "repo_path": REPO_A, "description": "Member rule", "rule_id": "alice-rule"})
    r.check(s, "member records a rule in the allowed project", status == 200, {"status": status, "body": body})
    status, body = api.call("POST", "/learnings", st.alice_key, json={
        "statement": "Member learning", "repo_scope": REPO_A})
    r.check(s, "member records a learning in the allowed project", status == 200, {"status": status})
    status, body = api.call("POST", "/decisions", st.alice_key, json={
        "title": "Alice on beta", "repo_path": REPO_B, "description": "Not her project"})
    r.check(s, "member denied writing to the other project", status == 403, {"status": status, "detail": detail(body)})
    status, body = api.call("POST", "/decisions", st.alice_key, json={"title": "Alice global", "description": "x"})
    r.check(s, "member denied writing system_global memory", status == 403, {"status": status, "detail": detail(body)})
    status, body = api.call("POST", "/decisions", st.alice_key, json={
        "title": "Use pgvector", "repo_path": REPO_A, "description": "Member edit", "expected_version": 2})
    r.check(s, "member edits a legacy decision in her project (CAS on version 2 -> 3)",
            status == 200 and body.get("version") == 3 and body.get("decision_id") == st.pgvector_decision, body)

    # A run delegation the member requests: its credential must die with her.
    status, task = api.call("POST", f"/team/projects/{st.project_core}/tasks", st.alice_key, json={
        "repository_id": st.repo_a, "title": "E2E task", "goal": "Exercise runs", "branch": "e2e/task",
        "base_sha": "a" * 40, "idempotency_key": "e2e-task-0001"})
    r.check(s, "member creates a team task", status == 200, {"status": status, "detail": detail(task)})
    st.task_id = task.get("id", "") if isinstance(task, dict) else ""
    status, run = api.call("POST", f"/team/projects/{st.project_core}/tasks/{st.task_id}/runs", st.alice_key, json={
        "branch": "e2e/run", "permissions": ["read"], "idempotency_key": "e2e-run-0001"})
    st.run_key = r.secret(run.get("credential") or "") if isinstance(run, dict) else ""
    r.check(s, "member requests a run and gets a run credential", status == 200 and st.run_key.startswith("pbk_"),
            {"status": status})
    status = api.status("GET", f"/team/projects/{st.project_core}/tasks", st.run_key)
    r.check(s, "run credential reads its project tasks (right after the run was created)", status == 200,
            {"status": status})

    # Bob: added by the operator (brain-credential identity), so he can approve team-scoped OAuth.
    added = env.cli(MAIN_DB, "member", "add", "--principal", "bob", "--mint-key", "--project", "core",
                    "--role", "contributor")
    st.bob_key = r.secret(shown_secret(added.stderr))
    st.bob_id = env.sql(MAIN_DB, "SELECT id FROM principals WHERE name='bob'")[0]["id"]
    grant = oauth_flow(api, st.bob_key, "team:read core:write", "e2e bob")
    ok = "access_token" in grant
    if ok:
        st.bob_access, st.bob_refresh = r.secret(grant["access_token"]), r.secret(grant["refresh_token"])
        st.oauth_client = grant["client_id"]
    r.check(s, "operator-added member completes team-scoped OAuth (PKCE, consent with her key)", ok,
            {k: v for k, v in grant.items() if "token" not in k})
    owner = env.sql(MAIN_DB, "SELECT p.name FROM api_credentials c JOIN principals p ON p.id=c.principal_id "
                             f"WHERE c.key_hash='{hashlib.sha256(st.bob_access.encode()).hexdigest()}'") if ok else []
    r.check(s, "team-scoped OAuth token is bound to the member's own principal", owner == [{"name": "bob"}], owner)
    r.check(s, "member OAuth token reads the allowed project",
            ok and api.status("GET", "/learnings", st.bob_access, params={"repo_path": REPO_A}) == 200)
    alice_team = oauth_flow(api, st.alice_key, "team:read core:write", "e2e alice team")
    # Gap: invited members are verified as brain-invitation/invitation:<id>, but the consent
    # page maps an API-key approver to brain-credential/<name> (brain/team/oauth_link.py
    # approver_identity), so member_grant finds no membership.
    r.gap(s, "invited member approves team-scoped OAuth with her key",
          "access_token" in alice_team,
            {k: v for k, v in alice_team.items() if "token" not in k})
    alice_personal = oauth_flow(api, st.alice_key, "core:write", "e2e alice personal")
    if "access_token" in alice_personal:
        st.alice_oauth_access = r.secret(alice_personal["access_token"])
        st.alice_oauth_refresh = r.secret(alice_personal["refresh_token"])
        st.alice_oauth_client = alice_personal["client_id"]
    owner = env.sql(MAIN_DB, "SELECT p.name, p.kind FROM api_credentials c JOIN principals p ON p.id=c.principal_id "
                             f"WHERE c.key_hash='{hashlib.sha256(st.alice_oauth_access.encode()).hexdigest()}'"
                    ) if st.alice_oauth_access else []
    personal_read = api.status("GET", "/learnings", st.alice_oauth_access, params={"repo_path": REPO_A}) \
        if st.alice_oauth_access else None
    r.check(s, "personal-scope OAuth approved by a member lands on a service principal without project access",
            bool(st.alice_oauth_access) and personal_read == 403, {"principal": owner, "read_alpha": personal_read})

    status, body = api.call("POST", "/search", st.alice_key, json={"query": "pgvector", "repo_path": REPO_A,
                                                                    "response_mode": "legacy"})
    r.check("5-freshness", "/search reply carries a freshness envelope",
            status == 200 and isinstance(body, dict) and isinstance(body.get("freshness"), dict)
            and body["freshness"].get("state") in {"fresh", "stale_served", "unknown"},
            {"status": status, "freshness": body.get("freshness") if isinstance(body, dict) else detail(body)})
    status, body = api.call("POST", "/context", st.alice_key, json={"task_description": "Where is pgvector used?",
                                                                     "repo_path": REPO_A})
    r.check("5-freshness", "/context reply carries a freshness envelope",
            status == 200 and isinstance(body, dict) and isinstance(body.get("freshness"), dict),
            {"status": status, "freshness": body.get("freshness") if isinstance(body, dict) else detail(body)})
    result = mcp_call(api.base, st.alice_key, "search_code", {"query": "pgvector", "repo_path": REPO_A})
    text = result.get("text") or ""
    try:
        payload = json.loads(text)
    except ValueError:
        payload = {}
    r.check("5-freshness", "MCP search_code reply (member key) carries a freshness envelope",
            not result.get("is_error") and isinstance(payload, dict) and isinstance(payload.get("freshness"), dict),
            {"freshness": payload.get("freshness") if isinstance(payload, dict) else None,
             "error": result.get("exception") or (text[:200] if result.get("is_error") else None)})
    r.check(s, "MCP refuses the shared key in team state", mcp_status(api.base, env.shared_key) == 401)


def scenario_4_restore(env: Env, st: State) -> None:
    r = env.report
    s = "4-restore"
    backup = env.work / "backup"
    shutil.rmtree(backup, ignore_errors=True)
    config = env.work / "backup.env"
    config.write_text(f"DATABASE_URL={env.url(MAIN_DB)}\nNEO4J_URI=bolt://127.0.0.1:1\nNEO4J_USER=fixture\n"
                      f"NEO4J_PASSWORD={env.neo4j_password}\nBRAIN_STORAGE_MODE=full\n", encoding="utf-8")
    clean = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "SYSTEMROOT", "TEMP", "TMP", "LANG"}}
    clean["PYTHONPATH"] = str(ROOT)
    proc = env.run([sys.executable, "scripts/backup_runner.py", "--config", str(config), "--output", str(backup),
                    "--allow-missing", "neo4j"], env=clean, cwd=ROOT, check=False)
    manifest = json.loads((backup / "backup_manifest.json").read_text()) if (backup / "backup_manifest.json").is_file() else {}
    r.check(s, "backup_runner writes a verified PostgreSQL dump", proc.returncode == 0 and manifest.get("status") == "ok",
            {"exit": proc.returncode, "status": manifest.get("status"),
             "artifacts": [(a.get("name"), a.get("status")) for a in manifest.get("artifacts", [])],
             "stderr": proc.stderr.strip()[-300:]})
    if proc.returncode != 0:
        return
    st.backup_dir = backup
    dump = (backup / "postgres_dump.sql").read_text(encoding="utf-8", errors="replace")
    r.check(s, "dump contains no plaintext member key", st.alice_key not in dump and st.admin_key not in dump)

    for database, keep in ((RESTORE_KEEP_DB, True), (RESTORE_REVOKE_DB, False)):
        label = "keep" if keep else "revoke"
        target = env.work / f"restore-{label}.env"
        target.write_text(config.read_text().replace(f"/{MAIN_DB}", f"/{database}"), encoding="utf-8")
        proc = env.run([sys.executable, "scripts/restore_database.py", str(backup), "--config", str(target),
                        "--confirm-database", database], env=clean, cwd=ROOT, check=False)
        r.check(s, f"[{label}] restore_database.py restores and reports the fence",
                proc.returncode == 0 and "FENCED" in (proc.stdout + proc.stderr),
                {"exit": proc.returncode, "out": (proc.stdout + proc.stderr).strip()[-300:]})
        if proc.returncode != 0:
            diag = env.psql(env.url(database), "-f", str(backup / "postgres_dump.sql"), check=False)
            r.check(s, f"[{label}] diagnosis: plain psql replay of the dump", diag.returncode == 0,
                    diag.stderr.strip()[-400:])
            continue
        api = env.start_api(database, env.args.api_port + 1, f"restore-{label}")
        try:
            _, health = api.call("GET", "/health")
            r.check(s, f"[{label}] /health team_access pending_reconciliation",
                    health.get("team_access") == "pending_reconciliation", health.get("team_access"))
            statuses = {name: api.status("GET", "/decisions", key) for name, key in (
                ("admin", st.admin_key), ("alice", st.alice_key), ("bob_oauth", st.bob_access),
                ("run", st.run_key), ("shared", env.shared_key), ("legacy_owner", st.owner_key))}
            r.check(s, f"[{label}] fenced: no credential honoured", all(v in {401, 403} for v in statuses.values()),
                    statuses)
            r.check(s, f"[{label}] fenced: OAuth refresh refused",
                    refresh(api, st.oauth_client, st.bob_refresh) >= 400 if st.oauth_client else False)
            review = env.cli_json(database, "reconcile")
            r.check(s, f"[{label}] reconcile review prints a digest", len(review.get("digest", "")) == 64,
                    {"members": len(review.get("members", [])), "to_revoke": review.get("to_revoke_unless_kept")})
            wrong = env.cli(database, "reconcile", "--attest", "0" * 64, check=False)
            _, health = api.call("GET", "/health")
            r.check(s, f"[{label}] a wrong digest leaves the fence in place",
                    wrong.returncode != 0 and health.get("team_access") == "pending_reconciliation",
                    wrong.stderr.strip()[-160:])
            argv = ["reconcile", "--attest", review["digest"]] + (["--keep-credentials"] if keep else [])
            result = env.cli_json(database, *argv)
            _, health = api.call("GET", "/health")
            r.check(s, f"[{label}] reconcile --attest clears the fence", health.get("team_access") == "open",
                    {"team_access": health.get("team_access"), "result": {k: result[k] for k in result
                                                                          if k != "digest"}})
            # A run credential carries team scopes only, so it is probed on its project's tasks.
            statuses = {name: api.status("GET", "/learnings", key, params={"repo_path": REPO_A}) for name, key in (
                ("admin", st.admin_key), ("alice", st.alice_key), ("bob_oauth", st.bob_access))}
            statuses["run"] = api.status("GET", f"/team/projects/{st.project_core}/tasks", st.run_key)
            if keep:
                r.check(s, "[keep] restored credentials work again after --keep-credentials",
                        all(v == 200 for v in statuses.values()), statuses)
            else:
                r.check(s, "[revoke] default reconcile revokes every restored credential",
                        all(v == 401 for v in statuses.values()), statuses)
                minted = env.cli(database, "member", "add", "--principal", "ops", "--issuer", "brain-invitation",
                                 "--subject", f"invitation:{admin_invitation_id(env, database)}", "--mint-key",
                                 "--admin")
                fresh_key = r.secret(shown_secret(minted.stderr))
                status, body = api.call("GET", "/api/web-session", fresh_key)
                r.check(s, "[revoke] re-minted admin key works (same issuer/subject)",
                        status == 200 and body.get("team_admin") is True, {"status": status})
            r.check(s, f"[{label}] shared key still refused (team state)",
                    api.status("GET", "/decisions", env.shared_key) == 403)
        finally:
            api.close()

    env.recreate(PLAIN_DB)
    env.psql(env.url(PLAIN_DB), "-q", "-f", str(backup / "postgres_dump.sql"))
    api = env.start_api(PLAIN_DB, env.args.api_port + 1, "plain")
    try:
        _, health = api.call("GET", "/health")
        statuses = {name: api.status("GET", "/learnings", key, params={"repo_path": REPO_A}) for name, key in (
            ("admin", st.admin_key), ("alice", st.alice_key))}
        r.check(s, "plain psql restore is NOT fenced (documented): restored keys honoured",
                health.get("team_access") == "open" and all(v == 200 for v in statuses.values()),
                {"team_access": health.get("team_access"), **statuses})
    finally:
        api.close()


def admin_invitation_id(env: Env, database: str) -> str:
    rows = env.sql(database, "SELECT verification_subject FROM team_members m JOIN principals p "
                             "ON p.id=m.principal_id WHERE p.name='ops'")
    return rows[0]["verification_subject"].split(":", 1)[1]


def scenario_3_revoke(env: Env, api: Api, st: State) -> None:
    r = env.report
    s = "3-revoke"
    status, body = api.call("DELETE", f"/team/projects/members/{st.alice_id}", st.admin_key)
    r.check(s, "admin revokes alice over the API", status == 200, {"status": status, "detail": detail(body)})
    statuses = {
        "member_key": api.status("GET", "/learnings", st.alice_key, params={"repo_path": REPO_A}),
        "web_session": api.status("GET", "/api/web-session", st.alice_key),
        "run_credential": api.status("GET", f"/team/projects/{st.project_core}/tasks", st.run_key),
        "mcp": mcp_status(api.base, st.alice_key),
    }
    r.check(s, "alice: member key, dashboard, run credential and MCP dead immediately",
            all(v == 401 for v in statuses.values()), statuses)
    if st.alice_oauth_access:
        # Gap: the personal-scope token alice approved belongs to a service principal
        # (oauth:<client>:...), not to her, so revoking her does not revoke it. Team state
        # keeps it powerless (403); see the post-dissolve check below.
        evidence: dict[str, Any] = {"GET /decisions": api.status("GET", "/decisions", st.alice_oauth_access)}
        evidence["refresh"], grant = refresh_grant(api, st.alice_oauth_client, st.alice_oauth_refresh)
        if grant.get("access_token"):
            # Refresh rotates: the new pair is what a client would hold from now on.
            st.alice_oauth_access = r.secret(grant["access_token"])
            st.alice_oauth_refresh = r.secret(grant.get("refresh_token") or "")
            evidence["refreshed_access_GET /decisions"] = api.status("GET", "/decisions", st.alice_oauth_access)
        r.gap(s, "alice's personal-scope OAuth credentials revoked with her (access 401, refresh refused)",
              evidence["GET /decisions"] == 401 and evidence["refresh"] >= 400, evidence)
    _, rows = api.call("GET", "/team/invitations", st.admin_key)
    state = next((row["state"] for row in rows if row["id"] == st.alice_invitation), "missing")
    r.check(s, "invitation state: revoked", state == "revoked", state)

    env.cli_json(MAIN_DB, "member", "revoke", "--principal", "bob")
    statuses = {"key": api.status("GET", "/learnings", st.bob_key, params={"repo_path": REPO_A}),
                "oauth_access": api.status("GET", "/learnings", st.bob_access, params={"repo_path": REPO_A}),
                "oauth_refresh": refresh(api, st.oauth_client, st.bob_refresh)}
    r.check(s, "bob (CLI revoke): key and OAuth access dead, refresh refused",
            statuses["key"] == 401 and statuses["oauth_access"] == 401 and statuses["oauth_refresh"] >= 400, statuses)

    env.cli_json(MAIN_DB, "member", "revoke", "--principal", "ops")
    _, health = api.call("GET", "/health")
    r.check(s, "last member revoked: Brain stays in team state", health.get("mode") == "team", health.get("mode"))
    r.check(s, "last member revoked: shared key still refused", api.status("GET", "/decisions", env.shared_key) == 403)
    r.check(s, "last member revoked: admin key dead", api.status("GET", "/team/invitations", st.admin_key) == 401)
    refused = env.cli(MAIN_DB, "dissolve", check=False)
    r.check(s, "dissolve without --yes is refused", refused.returncode != 0)
    env.cli_json(MAIN_DB, "dissolve", "--yes")
    _, health = api.call("GET", "/health")
    r.check(s, "brain team dissolve --yes returns the Brain to solo", health.get("mode") == "solo", health.get("mode"))
    r.check(s, "after dissolve the shared key works again", api.status("GET", "/decisions", env.shared_key) == 200)
    r.check(s, "after dissolve revoked member keys stay dead",
            api.status("GET", "/decisions", st.alice_key) == 401 and api.status("GET", "/decisions", st.admin_key) == 401)
    if st.alice_oauth_access:
        status = api.status("GET", "/decisions", st.alice_oauth_access)
        r.gap(s, "after dissolve a revoked member's personal OAuth token (latest rotation) stays dead",
              status == 401, {"GET /decisions": status, "note": "200 here means the token outlived its approver"})


# --------------------------------------------------------------------------- main

def preflight(args: argparse.Namespace) -> list[str]:
    problems = []
    for tool in ("psql", "pg_dump"):
        if not shutil.which(tool):
            problems.append(f"{tool} not found on PATH")
    legacy = Path(args.legacy_tree)
    if not (legacy / "brain" / "database" / "session.py").is_file():
        problems.append(f"--legacy-tree {legacy} is not an extracted {LEGACY_COMMIT} tree")
    elif (legacy / "brain" / "team").exists():
        problems.append(f"--legacy-tree {legacy} already has brain/team: not the {LEGACY_COMMIT} release")
    try:
        e2e_database_url(args.database_url, MAIN_DB)
    except ValueError as exc:
        problems.append(str(exc))
    return problems


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--database-url", default=os.getenv("TEAM_E2E_DATABASE_URL"),
                        help="Superuser URL of a disposable PostgreSQL server (postgresql://user:pass@host:port/postgres)")
    parser.add_argument("--redis-url", default=os.getenv("TEAM_E2E_REDIS_URL", "redis://127.0.0.1:6398/0"))
    parser.add_argument("--legacy-tree", default=os.getenv("TEAM_E2E_LEGACY_TREE"),
                        help=f"Extracted tree of {LEGACY_COMMIT} (git archive {LEGACY_COMMIT} | tar -x -C DIR)")
    parser.add_argument("--api-port", type=int, default=18471, help="Port of the main API (the next one is used too)")
    parser.add_argument("--work-dir", default=os.getenv("TEAM_E2E_WORK_DIR", str(ROOT / "artifacts" / "team-e2e")))
    parser.add_argument("--report", help="JSON report path (default: <work-dir>/report.json)")
    parser.add_argument("--keep-databases", action="store_true", help="Leave the brain_e2e_* databases for inspection")
    args = parser.parse_args(argv)
    if not args.database_url or not args.legacy_tree:
        parser.error("--database-url and --legacy-tree are required")
    return args


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    problems = preflight(args)
    if problems:
        for problem in problems:
            print(f"preflight: {problem}", file=sys.stderr)
        return 2
    report = Report()
    env = Env(args, report)
    st = State()
    api: Optional[Api] = None
    try:
        scenario_1_legacy_upgrade(env, st)
        api = env.start_api(MAIN_DB, args.api_port, "main")
        scenario_1_http(env, api, st)
        scenario_2_idempotent(env, st)
        scenario_3_team(env, api, st)
        scenario_4_restore(env, st)
        scenario_3_revoke(env, api, st)
    except Exception as exc:  # noqa: BLE001 - recorded, the report says where it stopped
        report.errors.append(f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-3000:]}")
        print(redact(report.errors[-1], report.secrets), file=sys.stderr)
    finally:
        if api is not None:
            api.close()
        env.stop()
        if not args.keep_databases:
            for database in (MAIN_DB, RESTORE_KEEP_DB, RESTORE_REVOKE_DB, PLAIN_DB):
                env.drop(database)
        path = Path(args.report) if args.report else env.work / "report.json"
        path.write_text(json.dumps(report.as_dict(), indent=2), encoding="utf-8")
        summary = report.as_dict()
        print(f"\nteam e2e: {summary['passed']} passed, {summary['failed']} failed, "
              f"{summary['known_gaps']} known gaps, {summary['gaps_fixed']} gaps fixed, "
              f"{len(summary['errors'])} errors; report: {path}")
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
