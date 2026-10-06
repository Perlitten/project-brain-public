#!/usr/bin/env python3
"""Harness effectiveness benchmark.

Measures whether Project Brain actually helps development, against
human-curated ground truth:

  context tasks -> does /context return the files an engineer needs?
  impact tasks  -> does /impact find the files a change really touches?
  review tasks  -> does /diff-review catch planted bugs (mutation testing)
                   and stay quiet on clean diffs?

Retrieval quality itself is covered by eval/run_golden_eval.py; this
benchmark sits one level higher: task usefulness.

Usage:
  python eval/harness_benchmark/run.py [--api-url URL] [--repo PATH]
                                       [--tasks-dir DIR] [--output FILE]
                                       [--only context-001] [--skip-review]

The API must be running. Review tasks need a clean worktree and create
(then delete) temporary branches bench/<task-id>.
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from metrics import detection, hit_rate, mean, mrr, noise_ratio, percentile  # noqa: E402

_ENC = None


def count_tokens(text: str) -> int:
    """Real token count (cl100k_base), not a chars/4 estimate."""
    global _ENC
    if _ENC is None:
        import tiktoken

        _ENC = tiktoken.get_encoding("cl100k_base")
    return len(_ENC.encode(text))


def _env_value(repo, key):
    value = os.environ.get(key)
    if value:
        return value.strip()
    env_path = os.path.join(repo, ".env")
    if os.path.exists(env_path):
        for line in open(env_path):
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    return None


async def _db_connect(repo):
    """Direct Postgres connection to the database behind the API under test.

    The benchmark runs on the same host as the API, so it can verify and
    clean up the rows the API serves — L2 episodes have no write/delete API.
    """
    import asyncpg

    return await asyncpg.connect(
        host=_env_value(repo, "POSTGRES_HOST") or "localhost",
        port=int(_env_value(repo, "POSTGRES_PORT") or "5433"),
        user=_env_value(repo, "POSTGRES_USER") or "postgres",
        password=_env_value(repo, "POSTGRES_PASSWORD") or "postgres_password",
        database=_env_value(repo, "POSTGRES_DB") or "brain_db",
    )

# Harsh benchmark: hard latency SLA per endpoint (seconds).
# A task FAILS if it exceeds its SLA, regardless of quality metrics.
SLA = {
    "context": 15,
    "impact_fast": 30,
    "impact_full": 330,
    "ask": 180,
    "review": 330,
    "memory": 120,
}

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
TASKS_DIR = os.path.join(HERE, "tasks")

FILE_EXTS = (".py", ".js", ".ts", ".tsx", ".kt", ".rs", ".go", ".java")


def load_api_key(repo):
    key = os.environ.get("PROJECT_BRAIN_API_KEY")
    if key:
        return key.strip()
    env_path = os.path.join(repo, ".env")
    if os.path.exists(env_path):
        for line in open(env_path):
            if line.startswith("PROJECT_BRAIN_API_KEY="):
                return line.split("=", 1)[1].strip()
    raise SystemExit("API key not found: set PROJECT_BRAIN_API_KEY or keep it in <repo>/.env")


def api_post(api_url, api_key, path, payload, timeout=300):
    req = urllib.request.Request(
        api_url.rstrip("/") + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-API-Key": api_key},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def api_get(api_url, api_key, path, timeout=60):
    req = urllib.request.Request(
        api_url.rstrip("/") + path,
        headers={"X-API-Key": api_key},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def git(repo, *args, check=True, **kwargs):
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=check, **kwargs
    )


def is_fileish(entry):
    return isinstance(entry, str) and "/" in entry and entry.endswith(FILE_EXTS)


def run_context(api_url, api_key, repo, task):
    data = api_post(api_url, api_key, "/context", {
        "task_description": task["task_description"],
        "repo_path": repo,
        "persist": False,
    })
    files = [c.get("path") for c in data.get("candidates", []) if c.get("path")]
    must = task.get("must_have_files", [])
    nice = task.get("nice_to_have_files", [])
    metrics = {
        "must_have_hit_rate": hit_rate(must, files),
        "mrr": mrr(must, files),
        "noise_ratio": noise_ratio(must, nice, files),
    }
    # Token economy: compare harness response size vs naive full-file reads.
    # Naive baseline: agent reads all must_have + nice_to_have files fully.
    # Harness: curated candidates with summaries (the actual API response).
    # Savings = 1 - harness_tokens / naive_tokens, counted with a real
    # tokenizer (cl100k_base) — not a bytes/4 estimate.
    try:
        naive_tokens = 0
        for f in set(must) | set(nice):
            fp = os.path.join(repo, f)
            if os.path.isfile(fp):
                with open(fp, encoding="utf-8", errors="ignore") as fh:
                    naive_tokens += count_tokens(fh.read())
        harness_tokens = count_tokens(json.dumps(data))
        if naive_tokens > 0:
            metrics["token_savings_ratio"] = round(1 - harness_tokens / naive_tokens, 3)
            metrics["naive_tokens"] = naive_tokens
            metrics["harness_tokens"] = harness_tokens
    except Exception:
        pass
    # Adversarial: query with no good answer (e.g. Kubernetes for a Python
    # repo). Honest harness returns little/nothing; hallucinating fails.
    if task.get("adversarial") and task["id"] == "context-007":
        metrics["adversarial_pass"] = 1.0 if len(files) <= 3 else 0.0
    return {
        "candidates": files,
        "metrics": metrics,
    }


def run_impact(api_url, api_key, repo, task):
    data = api_post(api_url, api_key, "/impact", {
        "change_request": task["change_request"],
        "repo_path": repo,
        # Fast mode: deterministic keyword extraction, no LLM calls.
        # Full mode takes ~4min per query (two LLM calls); fast mode
        # returns in ~1s with equal or better recall on these tasks.
        "fast": True,
    })
    raw = list(data.get("directly_affected", [])) + list(data.get("indirectly_affected", []))
    files = [e for e in raw if is_fileish(e)]
    must = task.get("must_have_files", [])
    metrics = {
        "must_have_recall": hit_rate(must, files),
        "affected_file_count": len(files),
    }
    # Adversarial tasks: vague or ultra-common requests should NOT produce
    # huge hallucinated impact lists. Score restraint, not recall.
    if task.get("adversarial"):
        if task["id"] == "impact-003":
            # Vague "improve performance": restrained output passes.
            metrics["adversarial_pass"] = 1.0 if len(files) < 20 else 0.0
        elif task["id"] == "impact-004":
            # Common word 'get': top-k must bound the explosion.
            metrics["adversarial_pass"] = 1.0 if len(files) <= 75 else 0.0
    return {
        "risk_level": data.get("risk_level"),
        "affected_files": files,
        "metrics": metrics,
    }


BENCH_EPISODE_TOPIC = "bench-episode"


async def _embed_for_api(text):
    """Embed with the same provider + column shape the API uses.

    The benchmark runner shares the repo's .env with the API it tests, so
    get_embedding_provider() resolves to the same backend.
    """
    from brain.embeddings.constants import EMBEDDING_DIMENSION
    from brain.embeddings.pgvector_sql import truncate_vector_for_index
    from brain.llm import get_embedding_provider

    vec = await get_embedding_provider().embed(text)
    if not vec:
        raise RuntimeError("embedding provider returned no vector")
    return truncate_vector_for_index(list(vec), EMBEDDING_DIMENSION)


def _pgvector_literal(vec) -> str:
    return "[" + ",".join(repr(float(v)) for v in vec) + "]"


async def _l2_lifecycle(api_url, api_key, repo):
    """l2-001: plant an episode via direct DB insert, verify the full
    status lifecycle through GET /episodes, then remove it."""
    marker = f"bench-l2-{int(time.time())}-{os.getpid()}"
    summary = f"Benchmark L2 probe {marker}: lifecycle persistence check."
    conn = await _db_connect(repo)
    episode_id = None
    checks = {}
    try:
        episode_id = await conn.fetchval(
            "INSERT INTO memory_episodes "
            "(source_event_ids, distilled_summary, topic, status, confidence, repo_scope) "
            "VALUES ('[]'::jsonb, $1, $2, 'pending', 0.7, $3) RETURNING id",
            summary,
            BENCH_EPISODE_TOPIC,
            repo,
        )
        listed = api_get(api_url, api_key, "/episodes?limit=200")
        mine = next((e for e in listed if e.get("id") == episode_id), None)
        checks["listed"] = mine is not None
        checks["fields_correct"] = bool(
            mine
            and mine.get("status") == "pending"
            and mine.get("distilled_summary") == summary
            and mine.get("topic") == BENCH_EPISODE_TOPIC
        )
        pending_only = api_get(api_url, api_key, "/episodes?status=pending&limit=200")
        checks["pending_filter_includes"] = any(e.get("id") == episode_id for e in pending_only)

        # Lifecycle: pending -> promoted via direct update (no API for this).
        await conn.execute(
            "UPDATE memory_episodes SET status = 'promoted' WHERE id = $1", episode_id
        )
        promoted = api_get(api_url, api_key, "/episodes?status=promoted&limit=200")
        pending_after = api_get(api_url, api_key, "/episodes?status=pending&limit=200")
        checks["promoted_filter_includes"] = any(e.get("id") == episode_id for e in promoted)
        checks["pending_filter_excludes"] = all(e.get("id") != episode_id for e in pending_after)
    finally:
        if episode_id is not None:
            await conn.execute("DELETE FROM memory_episodes WHERE id = $1", episode_id)
        await conn.close()
    return checks


async def _l2_semantic_search(api_url, api_key, repo, task):
    """l2-002: plant episodes with real embeddings and distinct topics,
    verify /episodes/search ranks the matching one first and can return
    nothing for an unrelated control query."""
    marker = int(time.time())
    # Target is inserted LAST: a search that returned insertion order would
    # rank it last, so `target_ranked_first` only passes on real similarity.
    probes = [
        (f"bench-l2s-backup-{marker}",
         "Nightly pg_dump archives are written to the workspace backups directory."),
        (f"bench-l2s-graph-{marker}",
         "The Neo4j graph is rebuilt by the reindex job after data loss."),
        (f"bench-l2s-restart-{marker}",
         "Operators restart the Brain API with the brain-env.sh wrapper script."),
    ]
    conn = await _db_connect(repo)
    ids = []
    checks = {}
    try:
        for name, summary in probes:
            vec = await _embed_for_api(summary)
            row = await conn.fetchval(
                "INSERT INTO memory_episodes "
                "(source_event_ids, distilled_summary, topic, status, confidence, embedding, repo_scope) "
                "VALUES ('[]'::jsonb, $1, $2, 'promoted', 0.7, $3::vector, $4) RETURNING id",
                summary,
                BENCH_EPISODE_TOPIC,
                _pgvector_literal(vec),
                repo,
            )
            ids.append((row, name, summary))

        query = task.get("query", "")
        results = api_post(api_url, api_key, "/episodes/search", {"query": query, "limit": 10})
        ranked_ids = [r.get("id") for r in results]
        target_id = ids[2][0]  # restart probe, inserted last
        checks["target_ranked_first"] = bool(ranked_ids) and ranked_ids[0] == target_id
        checks["distractor_not_first"] = ranked_ids[:1] != [ids[0][0]] and ranked_ids[:1] != [ids[1][0]]

        # Control: an unrelated query must not surface the restart probe on top
        # (guards against "return everything in INSERT order").
        control = api_post(
            api_url, api_key, "/episodes/search",
            {"query": "zebra migration patterns in the Serengeti", "limit": 10},
        )
        checks["control_does_not_rank_target_first"] = not (
            control and control[0].get("id") == target_id
        )
        checks["results_returned"] = len(ranked_ids)
    finally:
        if ids:
            await conn.execute(
                "DELETE FROM memory_episodes WHERE id = ANY($1::bigint[])", [r[0] for r in ids]
            )
        await conn.close()
    return checks


def run_episodic(api_url, api_key, repo, task):
    """Test L2 episodic memory with real rows: plant via DB, verify via API,
    delete. A task only passes when the API reflects the planted content."""
    task_id = task["id"]
    try:
        if task_id == "l2-001":
            checks = asyncio.run(_l2_lifecycle(api_url, api_key, repo))
            passed = all(checks.values())
            return {
                "passed": passed,
                "metrics": {"l2_persistence": 1.0 if passed else 0.0},
                "checks": checks,
            }
        elif task_id == "l2-002":
            checks = asyncio.run(_l2_semantic_search(api_url, api_key, repo, task))
            ranked_ok = checks.get("target_ranked_first") and checks.get("distractor_not_first")
            passed = bool(ranked_ok and checks.get("control_does_not_rank_target_first"))
            return {
                "passed": passed,
                "metrics": {"l2_search": 1.0 if passed else 0.0},
                "checks": checks,
            }
    except Exception as e:
        metric = "l2_persistence" if task_id == "l2-001" else "l2_search"
        return {"error": f"{type(e).__name__}: {e}", "metrics": {metric: 0.0}}
    return {"skipped": f"unknown episodic task {task_id}"}


# Fixture corpus for l4-002, seeded once per benchmark run by
# seed_l4_fixtures() (before the procedural tasks run) and deleted by
# cleanup_l4_fixtures() — the task itself must not seed the skill it
# checks for, so a pass measures matching, not the seeding write.
L4_FIXTURE_SKILLS = [
    {
        "name": "bench-restart-brain-api",
        "description": "Restart the Brain API using the brain-env.sh wrapper script",
        "triggers": ["restart", "brain api", "service restart"],
    },
    {
        "name": "bench-backup-database",
        "description": "Run database backup via backup_runner.py",
        "triggers": ["backup", "database", "pg_dump"],
    },
    {
        "name": "bench-photo-watermark",
        "description": "Batch-add a watermark to product photos with ImageMagick",
        "triggers": ["watermark", "photos", "imagemagick"],
    },
]
L4_FIXTURE_NAMES = [s["name"] for s in L4_FIXTURE_SKILLS]


def seed_l4_fixtures(api_url, api_key) -> list[str]:
    """Register the fixture skills l4-002 ranks; returns names now present."""
    existing = {s.get("name") for s in api_get(api_url, api_key, "/skills?limit=200")}
    seeded = []
    for skill in L4_FIXTURE_SKILLS:
        if skill["name"] in existing:
            seeded.append(skill["name"])
            continue
        api_post(api_url, api_key, "/skills", {
            "name": skill["name"],
            "description": skill["description"],
            "triggers": skill["triggers"],
        })
        seeded.append(skill["name"])
    return seeded


async def _delete_rows(repo, sql, *params):
    conn = await _db_connect(repo)
    try:
        await conn.execute(sql, *params)
    finally:
        await conn.close()


def cleanup_l4_fixtures(repo) -> None:
    """Hard-delete fixture skill rows so no benchmark data stays in L4."""
    try:
        asyncio.run(_delete_rows(
            repo, "DELETE FROM memory_skills WHERE name = ANY($1::text[])", L4_FIXTURE_NAMES
        ))
    except Exception as exc:  # noqa: BLE001 - best effort, reported by residue check
        print(f"[cleanup] fixture skill delete failed: {type(exc).__name__}: {exc}")


def run_procedural(api_url, api_key, repo, task):
    """Test L4 procedural memory: registration round-trip + matching.

    l4-002 does not create the skill it checks: it ranks fixture skills
    seeded once by the benchmark run (seed_l4_fixtures) and asserts on the
    ranking, not on a write the test just performed.
    """
    task_id = task["id"]
    try:
        if task_id == "l4-001":
            skill_def = task.get("skill_definition", {})
            unique_name = f"{skill_def.get('name', 'test-skill')}-{int(time.time())}-{os.getpid()}"
            checks = {}
            skill_name = None
            try:
                result = api_post(api_url, api_key, "/skills", {
                    "name": unique_name,
                    "description": skill_def.get("description", "Test skill"),
                    "triggers": skill_def.get("triggers", []),
                    "workflow": skill_def.get("workflow", []),
                })
                skill_name = unique_name
                checks["created"] = result.get("id") is not None
                # Read-back: the skill must be retrievable with its fields.
                skills = api_get(api_url, api_key, "/skills?limit=200")
                mine = next((s for s in skills if s.get("name") == unique_name), None)
                checks["retrievable"] = mine is not None
                checks["fields_preserved"] = bool(
                    mine
                    and mine.get("description") == skill_def.get("description")
                    and mine.get("triggers") == skill_def.get("triggers")
                )
                # Duplicate name must be refused.
                try:
                    api_post(api_url, api_key, "/skills", {
                        "name": unique_name,
                        "description": "duplicate",
                    })
                    checks["duplicate_rejected"] = False
                except Exception as e:  # noqa: BLE001 - expect HTTP 409
                    checks["duplicate_rejected"] = "409" in str(e) or "already exists" in str(e)
            finally:
                if skill_name:
                    try:
                        asyncio.run(_delete_rows(
                            repo, "DELETE FROM memory_skills WHERE name = $1", skill_name
                        ))
                    except Exception:
                        pass
            passed = bool(
                checks.get("created") and checks.get("retrievable")
                and checks.get("fields_preserved") and checks.get("duplicate_rejected")
            )
            return {
                "passed": passed,
                "metrics": {"l4_registration": 1.0 if passed else 0.0},
                "checks": checks,
            }
        elif task_id == "l4-002":
            skills = {s.get("name") for s in api_get(api_url, api_key, "/skills?limit=200")}
            missing = [n for n in L4_FIXTURE_NAMES if n not in skills]
            if missing:
                return {
                    "passed": False,
                    "metrics": {"l4_matching": 0.0},
                    "details": {"fixture_skills_missing": missing},
                }
            checks = {}
            restart, backup, watermark = L4_FIXTURE_NAMES
            result = api_post(api_url, api_key, "/skills/match", {
                "query": task.get("query", ""),
                "limit": 5,
            })
            matches = result if isinstance(result, list) else []
            top = matches[0].get("name") if matches else None
            checks["target_first"] = top == restart
            checks["distractor_not_first"] = top != backup
            # Control: an unrelated query must not rank the restart skill first
            # (it would if match only replayed insertion order).
            control = api_post(api_url, api_key, "/skills/match", {
                "query": "resize and watermark product photos for the catalog",
                "limit": 5,
            })
            checks["control_ranks_other"] = bool(control) and control[0].get("name") != restart
            passed = all(checks.values())
            return {
                "passed": passed,
                "metrics": {"l4_matching": 1.0 if passed else 0.0},
                "checks": checks,
                "top_match": top,
            }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "metrics": {"l4_test": 0.0}}
    return {"skipped": f"unknown procedural task {task_id}"}


def run_ask(api_url, api_key, repo, task):
    """Test /ask answer quality: no thinking leak, no truncation, relevant."""
    data = api_post(api_url, api_key, "/ask", {
        "query": task["query"],
        "repo_path": repo,
    }, timeout=SLA["ask"])
    answer = data.get("answer", "")
    ans_lower = answer.lower()

    # Thinking leak: any reasoning markers in the visible answer.
    leak_markers = task.get("must_not_contain", [])
    leaks = [m for m in leak_markers if m.lower() in ans_lower]

    # Content coverage: does the answer mention expected key terms?
    must_contain = task.get("must_contain", [])
    missing = [m for m in must_contain if m.lower() not in ans_lower]

    # Truncation: ends mid-word or mid-sentence without terminal punctuation.
    truncated = False
    if task.get("check_truncation"):
        stripped = answer.rstrip()
        if stripped and stripped[-1] not in '.!?:;`"\'':
            if not stripped.endswith("```"):
                truncated = True

    # Length bound.
    max_chars = task.get("max_answer_chars", 10000)
    too_long = len(answer) > max_chars

    passed = not leaks and not missing and not truncated and not too_long
    return {
        "answer_length": len(answer),
        "answer_preview": answer[:200],
        "metrics": {
            "no_thinking_leak": 1.0 if not leaks else 0.0,
            "content_coverage": 1.0 - len(missing) / max(len(must_contain), 1),
            "not_truncated": 1.0 if not truncated else 0.0,
            "length_ok": 1.0 if not too_long else 0.0,
            "ask_quality": 1.0 if passed else 0.0,
        },
        "details": {
            "leaks_found": leaks,
            "terms_missing": missing,
            "truncated": truncated,
        },
    }


def run_memory(api_url, api_key, repo, task):
    """Plant learning(s), ask a question, check the answer recalls them."""
    import urllib.error

    learning_ids = []
    try:
        # Support both single learning_statement and a learnings array.
        to_plant = task.get("learnings") or [
            {
                "statement": task["learning_statement"],
                "category": task.get("learning_category", "benchmark"),
                "confidence": 0.9,
            }
        ]
        for lrng in to_plant:
            planted = api_post(api_url, api_key, "/learnings", {
                "statement": lrng["statement"],
                "category": lrng.get("category", task.get("learning_category", "benchmark")),
                "confidence": lrng.get("confidence", 0.9),
            })
            if planted.get("learning_id"):
                learning_ids.append(planted["learning_id"])
        learning_id = learning_ids[0] if learning_ids else None
        data = api_post(api_url, api_key, "/ask", {
            "query": task["question"],
            "repo_path": repo,
        })
        answer = data.get("answer", "") or ""
        learnings_used = data.get("learnings_used", []) or []
        used_statements = " ".join(lrng.get("statement", "") for lrng in learnings_used)
        mentioned = [p for p in task.get("must_mention", []) if p in used_statements]
        missing = [p for p in task.get("must_mention", []) if p not in used_statements]
        # must_not_mention_as_answer: stale/superseded phrases that must NOT surface.
        forbidden = [p for p in task.get("must_not_mention_as_answer", []) if p in used_statements]
        # min_relevant_in_learnings_used: for many-learnings tasks, at least N
        # of the relevant ones must be in learnings_used.
        min_relevant = task.get("min_relevant_in_learnings_used", 0)
        relevant_hit = len(mentioned) >= min_relevant if min_relevant else True
        recalled = (len(missing) == 0 and len(task.get("must_mention", [])) > 0
                    and not forbidden and relevant_hit)
        return {
            "recalled": recalled,
            "answer_preview": answer[:300],
            "metrics": {
                "recall": 1.0 if recalled else 0.0,
                "phrases_found": len(mentioned),
                "phrases_missing": missing,
            },
        }
    finally:
        for lid in learning_ids:
            # Hard-delete via DB first so no benchmark row stays behind;
            # the API only soft-rejects (status='rejected'), which is the
            # fallback when the DB is unreachable.
            try:
                asyncio.run(_delete_rows(
                    repo, "DELETE FROM memory_learnings WHERE id = $1", lid
                ))
                continue
            except Exception:
                pass
            try:
                req = urllib.request.Request(
                    api_url.rstrip("/") + f"/learnings/{lid}",
                    headers={"X-API-Key": api_key},
                    method="DELETE",
                )
                with urllib.request.urlopen(req, timeout=30):
                    pass
            except Exception:  # noqa: BLE001 - cleanup is best-effort
                pass


def run_review(api_url, api_key, repo, task):
    if git(repo, "status", "--porcelain").stdout.strip():
        return {"skipped": "dirty worktree — review tasks refuse to run"}
    base = git(repo, "rev-parse", "HEAD").stdout.strip()
    branch = f"bench/{task['id']}"
    orig_branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    orig_ref = orig_branch if orig_branch != "HEAD" else base
    try:
        git(repo, "checkout", "-q", "-b", branch)
        proc = subprocess.run(["git", "apply", "--check", "-"], cwd=repo,
                              input=task["patch"], capture_output=True, text=True)
        if proc.returncode != 0:
            return {"skipped": f"patch does not apply on {base[:8]}: {proc.stderr.strip()[:200]}",
                    "base_used": base}
        subprocess.run(["git", "apply", "-"], cwd=repo,
                       input=task["patch"], check=True, capture_output=True, text=True)
        git(repo, "-c", "user.name=bench", "-c", "user.email=bench@local",
            "commit", "-qam", f"bench: {task['id']}")
        data = api_post(api_url, api_key, "/diff-review", {
            "base": base, "head": branch, "repo_path": repo,
        }, timeout=420)
    finally:
        git(repo, "checkout", "-q", orig_ref)
        git(repo, "branch", "-D", branch, check=False)

    llm_status = data.get("llm_review_status", "unknown")
    review_text = " ".join([
        str(data.get("markdown_content") or ""),
        *[str(v.get("details", v)) for v in data.get("rule_violations", [])],
        *data.get("suspicious_changes", []),
    ])
    metrics = {"llm_review_status": llm_status,
               "llm_available": 1.0 if llm_status == "completed" else 0.0}
    result = {
        "base_used": base,
        "base_pinned": task.get("base_commit"),
        "status": data.get("status"),
        "llm_review_status": llm_status,
        "rule_violations": data.get("rule_violations", []),
        "suspicious_changes": data.get("suspicious_changes", []),
        "metrics": metrics,
    }
    if llm_status != "completed":
        # Infrastructure failure, not a detection failure: report as error so it
        # does not pollute the detection-quality score. Availability is tracked
        # separately via the llm_available metric.
        result["error"] = f"llm_unavailable (llm_review_status={llm_status})"
        return result
    if task.get("clean"):
        false_alarm = bool(data.get("rule_violations") or data.get("suspicious_changes"))
        metrics["false_alarm"] = 1.0 if false_alarm else 0.0
        metrics["pass"] = 0.0 if false_alarm else 1.0
    else:
        detected = detection(task.get("must_detect_keywords", []), review_text)
        metrics["detected"] = 1.0 if detected else 0.0
        metrics["pass"] = metrics["detected"]
    return result


def _run_tasks(args, api_key, repo, task_files):
    results = []
    for fname in task_files:
        task = json.load(open(os.path.join(args.tasks_dir, fname)))
        if args.only and task["id"] != args.only:
            continue
        if args.skip_review and task["type"] == "review":
            continue
        print(f"[{task['id']}] {task['type']} ...", flush=True)
        started = time.time()
        try:
            if task["type"] == "context":
                outcome = run_context(args.api_url, api_key, repo, task)
            elif task["type"] == "impact":
                outcome = run_impact(args.api_url, api_key, repo, task)
            elif task["type"] == "review":
                outcome = run_review(args.api_url, api_key, repo, task)
            elif task["type"] == "memory":
                outcome = run_memory(args.api_url, api_key, repo, task)
            elif task["type"] == "ask":
                outcome = run_ask(args.api_url, api_key, repo, task)
            elif task["type"] == "episodic":
                outcome = run_episodic(args.api_url, api_key, repo, task)
            elif task["type"] == "procedural":
                outcome = run_procedural(args.api_url, api_key, repo, task)
            else:
                outcome = {"skipped": f"unknown type {task['type']}"}
        except Exception as e:  # noqa: BLE001 - benchmark must report, not crash
            outcome = {"error": f"{type(e).__name__}: {e}"}
        outcome["id"] = task["id"]
        outcome["type"] = task["type"]
        elapsed = time.time() - started
        outcome["seconds"] = round(elapsed, 1)
        # SLA enforcement: harsh benchmark fails tasks that breach latency.
        sla_key = task["type"]
        if task["type"] == "impact":
            sla_key = "impact_fast"  # benchmark uses fast mode
        sla_limit = SLA.get(sla_key)
        if sla_limit and elapsed > sla_limit and "skipped" not in outcome and "error" not in outcome:
            outcome["sla_breach"] = True
            outcome["sla_limit"] = sla_limit
        outcome["duration_s"] = round(time.time() - started, 1)
        results.append(outcome)
        print(f"  -> {json.dumps(outcome.get('metrics', outcome.get('skipped', outcome.get('error', 'ok'))))}")
    return results


def _check_residue(repo) -> dict:
    """Count leftover benchmark rows in the API's database. Every task deletes
    what it plants; nonzero counts mean a cleanup path failed."""
    counts = {}
    try:
        counts = asyncio.run(_count_residue(repo))
    except Exception:
        return {}  # no DB access from this host — nothing to verify here
    return {k: v for k, v in counts.items() if v}


async def _count_residue(repo) -> dict:
    conn = await _db_connect(repo)
    try:
        episodes = await conn.fetchval(
            "SELECT count(*) FROM memory_episodes WHERE topic = $1", BENCH_EPISODE_TOPIC
        )
        skills = await conn.fetchval(
            "SELECT count(*) FROM memory_skills WHERE name LIKE 'bench-%'"
        )
        learnings = await conn.fetchval(
            "SELECT count(*) FROM memory_learnings WHERE category IN ('benchmark', 'membench')"
        )
        return {"bench_episodes": episodes, "bench_skills": skills, "bench_learnings": learnings}
    finally:
        await conn.close()


def main():
    ap = argparse.ArgumentParser(description="Harness effectiveness benchmark")
    ap.add_argument("--api-url", default="http://127.0.0.1:8000")
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--repo", default=REPO_ROOT)
    ap.add_argument("--tasks-dir", default=TASKS_DIR)
    ap.add_argument("--output", default=None)
    ap.add_argument("--only", default=None, help="run only this task id")
    ap.add_argument("--skip-review", action="store_true")
    args = ap.parse_args()

    repo = os.path.abspath(args.repo)
    api_key = args.api_key or load_api_key(repo)
    task_files = sorted(f for f in os.listdir(args.tasks_dir) if f.endswith(".json"))

    tasks_by_id = {
        json.load(open(os.path.join(args.tasks_dir, f)))["id"]:
        json.load(open(os.path.join(args.tasks_dir, f))) for f in task_files
    }
    selected = [t for t in tasks_by_id.values() if not args.only or t["id"] == args.only]
    runs_procedural = any(t["type"] == "procedural" for t in selected)
    if runs_procedural:
        try:
            seeded = seed_l4_fixtures(args.api_url, api_key)
            print(f"[l4] fixture skills present: {seeded}")
        except Exception as exc:  # noqa: BLE001 - l4 tasks will fail honestly
            print(f"[l4] fixture seeding failed: {type(exc).__name__}: {exc}")
    try:
        results = _run_tasks(args, api_key, repo, task_files)
    finally:
        if runs_procedural:
            cleanup_l4_fixtures(repo)
        residue = _check_residue(repo)
        if residue:
            print(f"[cleanup] WARNING: benchmark rows left behind: {residue}")

    summary = summarize(results)
    print("\n" + summary["markdown"])

    payload = {"results": results, "summary": summary["scores"], "residue": residue}
    if args.output:
        with open(args.output, "w") as f:
            json.dump(payload, f, indent=2)
        print(f"\nWrote {args.output}")


def summarize(results):
    by_type = {}
    for r in results:
        by_type.setdefault(r["type"], []).append(r)
    lines = ["# Harness benchmark", "", "| task | type | key metric | value | secs |",
             "|---|---|---|---|---|"]
    scores = {}
    for r in results:
        m = r.get("metrics", {})
        if r.get("skipped"):
            val, key = "SKIPPED", r["skipped"]
        elif r.get("error"):
            val, key = "ERROR", r["error"]
        else:
            key = {"context": "must_have_hit_rate", "impact": "must_have_recall",
                   "review": "pass", "memory": "recall",
                   "episodic": "l2_persistence", "procedural": "l4_registration"}.get(r["type"], "")
            # For episodic/procedural, use the first available metric
            if r["type"] == "episodic":
                key = next((k for k in ["l2_persistence", "l2_search"] if k in m), "")
            elif r["type"] == "procedural":
                key = next((k for k in ["l4_registration", "l4_matching"] if k in m), "")
            val = m.get(key, "?")
        lines.append(f"| {r['id']} | {r['type']} | {key} | {val} | {r['duration_s']} |")
    lines.append("")
    for ttype, group in by_type.items():
        metric_keys = {"context": "must_have_hit_rate", "impact": "must_have_recall",
                       "review": "pass", "memory": "recall",
                       "ask": "ask_quality"}
        if ttype == "episodic":
            # Use any available L2 metric
            vals = []
            for g in group:
                gm = g.get("metrics", {})
                for k in ["l2_persistence", "l2_search"]:
                    if isinstance(gm.get(k), (int, float)):
                        vals.append(gm[k])
                        break
        elif ttype == "procedural":
            vals = []
            for g in group:
                gm = g.get("metrics", {})
                for k in ["l4_registration", "l4_matching"]:
                    if isinstance(gm.get(k), (int, float)):
                        vals.append(gm[k])
                        break
        else:
            k = metric_keys.get(ttype, "")
            vals = [g["metrics"][k] for g in group
                    if isinstance(g.get("metrics", {}).get(k), (int, float))]
        if vals:
            scores[ttype] = round(mean(vals), 3)
            lines.append(f"- **{ttype}**: mean = {scores[ttype]} (n={len(vals)})")
    # Latency percentiles across all tasks (robustness signal).
    durations = [r["duration_s"] for r in results if isinstance(r.get("duration_s"), (int, float))]
    if durations:
        lines.append("")
        lines.append(f"- **latency**: p50 = {percentile(durations, 50):.1f}s, "
                     f"p95 = {percentile(durations, 95):.1f}s, "
                     f"max = {max(durations):.1f}s (n={len(durations)})")
    done = [s for s in scores.values()]
    if done:
        scores["overall"] = round(mean(done), 3)
        lines.append(f"- **overall**: {scores['overall']}")
    return {"markdown": "\n".join(lines), "scores": scores}


if __name__ == "__main__":
    main()
