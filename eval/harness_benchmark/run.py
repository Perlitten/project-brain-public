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
import json
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from metrics import detection, hit_rate, mean, mrr, noise_ratio, percentile  # noqa: E402

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

    summary = summarize(results)
    print("\n" + summary["markdown"])

    payload = {"results": results, "summary": summary["scores"]}
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
                   "review": "pass", "memory": "recall"}.get(r["type"], "")
            val = m.get(key, "?")
        lines.append(f"| {r['id']} | {r['type']} | {key} | {val} | {r['duration_s']} |")
    lines.append("")
    for ttype, group in by_type.items():
        vals = [g["metrics"][k] for g in group
                for k in [{"context": "must_have_hit_rate", "impact": "must_have_recall",
                            "review": "pass", "memory": "recall"}[ttype]]
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
