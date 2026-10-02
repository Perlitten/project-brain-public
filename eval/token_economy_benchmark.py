#!/usr/bin/env python3
"""Compare complete Brain-first and local locator→targeted-read task loops.

The script refuses to label a synthetic/curated fixture as an acceptance run.
Schema-v2 production tasks must explicitly define file, symbol, range and
completion assertions. Both strategies then pay for a locator and the exact
code reads needed to complete the same evidence check.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import platform
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from typing import Any

from token_economy_schema import (
    DEFAULT_MANIFEST_PATH,
    PROJECT_ROOT,
    TokenEconomyTask,
    is_literal_assertion,
    load_tasks,
)


STOPWORDS = {
    "about", "after", "and", "are", "does", "for", "from", "how", "into", "is", "its", "of",
    "or", "the", "this", "to", "what", "where", "which", "with",
}


def _estimate_tokens(byte_count: int) -> int:
    return math.ceil(max(0, byte_count) / 4)


def _normalize_path(value: str) -> str:
    value = value.replace("\\", "/").strip()
    if value.startswith("/app/"):
        value = value[5:]
    return value.lstrip("/")


def _matches_expected(path: str, expected: str) -> bool:
    candidate, target = _normalize_path(path), _normalize_path(expected)
    return candidate == target or candidate.endswith("/" + target)


def _overlaps(left: tuple[int, int], right: tuple[int, int]) -> bool:
    return left[0] <= right[1] and right[0] <= left[1]


def _query_terms(query: str) -> list[str]:
    terms = re.findall(r"[A-Za-z_][A-Za-z0-9_./-]{2,}", query.lower())
    return list(dict.fromkeys(term for term in terms if term not in STOPWORDS))[:5]


def _read_targeted(root: Path, locator: dict[str, Any], max_bytes: int, padding: int) -> tuple[bytes, list[int]]:
    """Read only locator ranges (with small line padding), never a whole file."""
    path = str(locator.get("path") or "")
    if not path:
        return b"", []
    candidate = root / _normalize_path(path)
    try:
        content = candidate.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return b"", []
    lines = content.splitlines(keepends=True)
    ranges = [item for item in locator.get("ranges", []) if isinstance(item, list) and len(item) == 2]
    if not ranges:
        return b"", []
    selected: list[str] = []
    covered: list[int] = []
    for start, end in ranges[:8]:
        if not isinstance(start, int) or not isinstance(end, int) or start < 1 or end < start:
            continue
        first, last = max(1, start - padding), min(len(lines), end + padding)
        if first > last:
            continue
        fragment = "".join(lines[first - 1:last]).encode("utf-8")
        remaining = max_bytes - sum(len(item.encode("utf-8")) for item in selected)
        if remaining <= 0:
            break
        selected.append(fragment[:remaining].decode("utf-8", errors="ignore"))
        covered.extend(range(first, last + 1))
    return "".join(selected).encode("utf-8"), covered


def _outcome(
    locators: list[dict[str, Any]],
    reads: list[dict[str, Any]],
    task: TokenEconomyTask,
    root: Path | None = None,
) -> dict[str, Any]:
    paths = [str(locator["path"]) for locator in locators if locator.get("path")]
    first_rank = next((index for index, path in enumerate(paths, 1) if any(_matches_expected(path, target) for target in task.expected_files)), 0)
    mandatory_files_found = sorted(
        target for target in task.expected_files if any(_matches_expected(path, target) for path in paths)
    )
    read_text = b"".join(item["bytes"] for item in reads).decode("utf-8", errors="ignore").casefold()
    found_symbols = {
        str(symbol)
        for locator in locators
        for symbol in locator.get("symbols", [])
        if isinstance(symbol, str)
    }
    found_symbols.update(
        symbol for symbol in task.expected_symbols if symbol.casefold() in read_text
    )
    expected_symbols_found = sorted(set(task.expected_symbols) & found_symbols)
    mandatory_ranges_found: list[list[Any]] = []
    for path, start, end in task.expected_ranges:
        if any(
            _matches_expected(str(locator.get("path") or ""), path)
            and any(
                isinstance(item, list) and len(item) == 2
                and isinstance(item[0], int) and isinstance(item[1], int)
                and _overlaps((start, end), (item[0], item[1]))
                for item in locator.get("ranges", [])
            )
            for locator in locators
        ):
            mandatory_ranges_found.append([path, start, end])
    file_texts: dict[str, str] = {}
    if root is not None:
        for expected in task.expected_files:
            candidate = root / _normalize_path(expected)
            try:
                file_texts[expected] = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                file_texts[expected] = ""
        file_texts = {k: v for k, v in file_texts.items() if v}
    # Verbatim grounding: an assertion is scorable only if it occurs
    # verbatim in an expected file at the evaluated revision. Prose and
    # stale literals are unscored, never counted in coverage.
    literal_assertions = [
        a for a in task.required_assertions
        if is_literal_assertion(a, file_texts if root is not None else None)
    ]
    unscored_assertions = [a for a in task.required_assertions if a not in literal_assertions]
    assertions_found = sorted(
        assertion for assertion in literal_assertions if assertion.casefold() in read_text
    )
    assertions_missing = sorted(set(literal_assertions) - set(assertions_found))
    # Classify each miss: the literal is verbatim in an expected file — does
    # it sit inside an expected range (reachable by a correct locator) or
    # only outside them (unreachable under the targeted-read contract)?
    missing_where: dict[str, str] = {}
    if root is not None and assertions_missing:
        range_text = ""
        for range_file, start, end in task.expected_ranges:
            lines = file_texts.get(range_file, "").splitlines()
            range_text += "\n".join(lines[start - 1:end]) + "\n"
        for assertion in assertions_missing:
            missing_where[assertion] = (
                "in_expected_range_not_read" if assertion in range_text else "outside_expected_ranges"
            )
    evidence_complete = (
        set(mandatory_files_found) == set(task.expected_files)
        and set(expected_symbols_found) == set(task.expected_symbols)
        and {tuple(item) for item in mandatory_ranges_found} == set(task.expected_ranges)
        and set(assertions_found) == set(literal_assertions)
    )
    failure_reasons: list[str] = []
    if len(mandatory_files_found) != len(task.expected_files):
        failure_reasons.append(
            "files_missing:" + ",".join(sorted(set(task.expected_files) - set(mandatory_files_found)))
        )
    if len(expected_symbols_found) != len(task.expected_symbols):
        failure_reasons.append(
            "symbols_missing:" + ",".join(sorted(set(task.expected_symbols) - set(expected_symbols_found)))
        )
    if len(mandatory_ranges_found) != len(task.expected_ranges):
        failure_reasons.append(
            f"ranges_missing:{len(task.expected_ranges) - len(mandatory_ranges_found)}"
        )
    if assertions_missing:
        failure_reasons.append("literal_assertions_missing:" + ",".join(assertions_missing))
    if unscored_assertions:
        failure_reasons.append(f"assertions_not_verbatim_unscored:{len(unscored_assertions)}")
    return {
        "paths": paths,
        "expected_file_rank": first_rank,
        "hit_at_3_any": 1 <= first_rank <= 3,
        "mandatory_files_found": mandatory_files_found,
        "expected_symbols_found": expected_symbols_found,
        "mandatory_ranges_found": mandatory_ranges_found,
        "completion_assertions_found": assertions_found,
        "literal_assertion_total": len(literal_assertions),
        "unscored_assertion_total": len(unscored_assertions),
        "literal_assertions_missing": missing_where,
        "evidence_complete": evidence_complete,
        "failure_reasons": failure_reasons,
        "terminal_outcome": "completed" if evidence_complete else ("partial" if paths else "not_found"),
    }


def _local_rg_read(task: TokenEconomyTask, root: Path, result_limit: int, read_max_bytes: int, padding: int) -> dict[str, Any]:
    started = time.perf_counter()
    locators: dict[str, dict[str, Any]] = {}
    search_calls = 0
    root_resolved = root.resolve()
    terms = _query_terms(task.question)
    use_rg = True
    for term in terms:
        search_calls += 1
        if use_rg:
            try:
                completed = subprocess.run(
                    ["rg", "--line-number", "--no-heading", "--glob", "!*.pyc", "--glob", "!node_modules/**", term, str(root)],
                    capture_output=True, text=True, check=False, encoding="utf-8", errors="replace",
                )
            except FileNotFoundError:
                use_rg = False
        if not use_rg:
            # Python fallback: scan files for the term
            completed = None
            skip_dirs = {".git", "__pycache__", "node_modules", ".venv", ".mypy_cache", ".pytest_cache", "reports"}
            skip_exts = {".pyc", ".pyo", ".so", ".dll", ".exe", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf"}
            for file_path in root_resolved.rglob("*"):
                if not file_path.is_file():
                    continue
                if any(part in skip_dirs for part in file_path.parts):
                    continue
                if file_path.suffix in skip_exts:
                    continue
                try:
                    rel = file_path.relative_to(root_resolved).as_posix()
                except ValueError:
                    continue
                try:
                    content_lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
                except OSError:
                    continue
                term_lower = term.lower()
                for line_no, line in enumerate(content_lines, 1):
                    if term_lower in line.lower():
                        locator = locators.setdefault(rel, {"path": rel, "symbols": [], "ranges": []})
                        item = [line_no, line_no]
                        if item not in locator["ranges"]:
                            locator["ranges"].append(item)
                        break
                if len(locators) >= result_limit:
                    break
            continue
        for line in completed.stdout.splitlines():
            match = re.match(r"^(.+):(\d+):", line)
            if not match:
                continue
            raw_path, line_number = match.group(1), int(match.group(2))
            try:
                path = Path(raw_path).resolve().relative_to(root_resolved).as_posix()
            except ValueError:
                continue
            locator = locators.setdefault(path, {"path": path, "symbols": [], "ranges": []})
            item = [line_number, line_number]
            if item not in locator["ranges"]:
                locator["ranges"].append(item)
            if len(locators) >= result_limit:
                break
        if len(locators) >= result_limit:
            break
    ordered = list(locators.values())[:result_limit]
    reads = []
    for locator in ordered:
        raw, _covered = _read_targeted(root, locator, read_max_bytes, padding)
        if raw:
            reads.append({"path": locator["path"], "bytes": raw})
    locator_bytes = len(json.dumps(ordered, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    read_bytes = sum(len(item["bytes"]) for item in reads)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    result = {
        "strategy": "local_rg_read",
        "locators": ordered,
        "locator_bytes": locator_bytes,
        "read_bytes": read_bytes,
        "retrieved_bytes": locator_bytes + read_bytes,
        "estimated_tokens": _estimate_tokens(locator_bytes + read_bytes),
        "tool_calls": search_calls + len(reads),
        "latency_ms": elapsed_ms,
    }
    result.update(_outcome(ordered, reads, task, root))
    return result


def _brain_locators(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract locators from either v2 locator or legacy /search response format.

    v2 locator format: top-level ``results`` list with ``{path, symbols, ranges}``.
    Legacy format: separate ``files``, ``symbols``, ``chunks`` channels where
    ``symbols`` and ``chunks`` carry ``start_line``/``end_line`` that we map to
    locator ranges.
    """
    results = response.get("results")
    if isinstance(results, list):
        locators: list[dict[str, Any]] = []
        for item in results:
            if not isinstance(item, dict) or not item.get("path"):
                continue
            locators.append({
                "path": str(item["path"]),
                "symbols": [str(value) for value in item.get("symbols", []) if isinstance(value, str)],
                "ranges": [item for item in item.get("ranges", []) if isinstance(item, list) and len(item) == 2],
            })
        return locators

    # Legacy /search format — map files/symbols/chunks to locator dicts.
    by_path: dict[str, dict[str, Any]] = {}
    for item in response.get("files", []) or []:
        if not isinstance(item, dict) or not item.get("path"):
            continue
        path = str(item["path"])
        by_path.setdefault(path, {"path": path, "symbols": [], "ranges": []})
    for item in response.get("symbols", []) or []:
        if not isinstance(item, dict) or not item.get("path"):
            continue
        path = str(item["path"])
        locator = by_path.setdefault(path, {"path": path, "symbols": [], "ranges": []})
        name = item.get("name")
        if isinstance(name, str) and name not in locator["symbols"]:
            locator["symbols"].append(name)
        start, end = item.get("start_line"), item.get("end_line")
        if isinstance(start, int) and isinstance(end, int) and end >= start:
            r = [start, end]
            if r not in locator["ranges"]:
                locator["ranges"].append(r)
    for item in response.get("chunks", []) or []:
        path = str(item.get("file_path") or item.get("path") or "")
        if not path:
            continue
        locator = by_path.setdefault(path, {"path": path, "symbols": [], "ranges": []})
        start, end = item.get("start_line"), item.get("end_line")
        if isinstance(start, int) and isinstance(end, int) and end >= start:
            r = [start, end]
            if r not in locator["ranges"]:
                locator["ranges"].append(r)
    return list(by_path.values())


async def _brain_first(
    api_url: str, api_key: str, repo_path: str, root: Path, task: TokenEconomyTask,
    limit: int, read_max_bytes: int, padding: int, timeout_seconds: float,
) -> dict[str, Any]:
    started = time.perf_counter()
    status, payload, raw = await asyncio.to_thread(
        _request_json,
        api_url.rstrip("/") + "/search",
        {"X-API-Key": api_key},
        {"query": task.question, "limit": limit, "max_tokens": 600, "repo_path": repo_path, "response_mode": "locator"},
        timeout_seconds,
    )
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    if not 200 <= status < 300:
        raise RuntimeError(f"/search returned HTTP {status}")
    locators = _brain_locators(payload)
    if not locators:
        raise RuntimeError("locator_contract_missing_results")
    reads = []
    for locator in locators[:limit]:
        data, _covered = _read_targeted(root, locator, read_max_bytes, padding)
        if data:
            reads.append({"path": locator["path"], "bytes": data})
    read_bytes = sum(len(item["bytes"]) for item in reads)
    result = {
        "strategy": "brain_first",
        "locators": locators,
        "locator_bytes": len(raw),
        "read_bytes": read_bytes,
        "retrieved_bytes": len(raw) + read_bytes,
        "estimated_tokens": _estimate_tokens(len(raw) + read_bytes),
        "tool_calls": 1 + len(reads),
        "latency_ms": elapsed_ms,
        "freshness": (payload.get("repo") or {}).get("freshness") or (payload.get("repository_scope") or {}).get("freshness"),
    }
    result.update(_outcome(locators, reads, task, root))
    return result


def _request_json(url: str, headers: dict[str, str], body: dict[str, Any] | None, timeout_seconds: float) -> tuple[int, dict[str, Any], bytes]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request_headers = {"Accept": "application/json", **headers}
    if data is not None:
        request_headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=request_headers, method="POST" if data is not None else "GET")
    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # nosec B310 -- operator-provided URL
            raw = response.read()
            return int(response.status), json.loads(raw.decode("utf-8")), raw
    except HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
        return int(exc.code), payload, raw
    except URLError as exc:
        raise RuntimeError(f"HTTP request failed: {exc.reason}") from exc


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = [row["result"] for row in rows]
    def percentile(key: str, p: float) -> int | float:
        ordered = sorted(value[key] for value in values)
        return ordered[min(len(ordered) - 1, math.ceil(len(ordered) * p) - 1)] if ordered else 0
    mandatory_files = sum(len(row["task"]["expected_files"]) for row in rows)
    mandatory_symbols = sum(len(row["task"]["expected_symbols"]) for row in rows)
    mandatory_ranges = sum(len(row["task"]["expected_ranges"]) for row in rows)
    literal_assertions = sum(row["result"].get("literal_assertion_total", 0) for row in rows)
    semantic_assertions = sum(row["result"].get("unscored_assertion_total", 0) for row in rows)
    return {
        "count": len(values),
        "hit_at_3_any": round(sum(value["hit_at_3_any"] for value in values) / max(len(values), 1), 4),
        "evidence_complete_rate": round(sum(value["evidence_complete"] for value in values) / max(len(values), 1), 4),
        "metric_scope": (
            "retrieval proxy only: literal assertions are substring checks in locator-window reads; "
            "behavioral assertions are unscored; this is not observed coding-task completion"
        ),
        "mandatory_file_recall": round(sum(len(value["mandatory_files_found"]) for value in values) / max(mandatory_files, 1), 4),
        "expected_symbol_recall": round(sum(len(value["expected_symbols_found"]) for value in values) / max(mandatory_symbols, 1), 4),
        "mandatory_range_recall": round(sum(len(value["mandatory_ranges_found"]) for value in values) / max(mandatory_ranges, 1), 4),
        "literal_assertion_coverage": round(
            sum(len(value["completion_assertions_found"]) for value in values) / max(literal_assertions, 1), 4
        ),
        "literal_assertion_total": literal_assertions,
        "semantic_assertions_unscored_total": semantic_assertions,
        "median_total_context_tokens": percentile("estimated_tokens", 0.5),
        "p75_total_context_tokens": percentile("estimated_tokens", 0.75),
        "p95_latency_ms": percentile("latency_ms", 0.95),
        "mean_tool_calls": round(sum(value["tool_calls"] for value in values) / max(len(values), 1), 2),
    }


def _read_api_key(path: Path | None) -> str:
    value = os.environ.get("BRAIN_API_KEY", "").strip()
    if value:
        return value
    if path and path.is_file():
        return path.read_text(encoding="utf-8").strip()
    raise ValueError("Set BRAIN_API_KEY or provide --api-key-file for Brain-first")


def _local_head_commit(root: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _files_changed_since(root: Path, base_commit: str, files: list[str]) -> list[str] | None:
    """Files in ``files`` that differ between base_commit and the worktree.

    Returns None when the comparison cannot be performed (pin unknown to git).
    """
    if not files:
        return []
    try:
        proc = subprocess.run(  # noqa: S603
            ["git", "-C", str(root), "diff", "--name-only", base_commit, "--", *sorted(set(files))],
            capture_output=True, text=True, timeout=60, check=False,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _pin_check(tasks: list[Any], corpus: dict[str, Any], root: Path | None) -> dict[str, Any]:
    pinned = ((corpus.get("repository") or {}).get("pinned_commit")) or None
    check: dict[str, Any] = {"pinned_commit": pinned}
    if root is None or not pinned:
        check["aligned"] = None if pinned else True
        return check
    head = _local_head_commit(root)
    check["local_head"] = head
    changed = _files_changed_since(root, pinned, [f for t in tasks for f in t.expected_files])
    if changed is None:
        check["aligned"] = None
        check["alignment_mode"] = "pin_not_comparable"
        return check
    check["aligned"] = not changed
    check["alignment_mode"] = (
        "expected_files_differ" if changed else
        "head_equals_pin" if head == pinned else "expected_files_unchanged"
    )
    if changed:
        check["changed_expected_files"] = changed
    return check


async def _run_once(args: argparse.Namespace, strategies: list[str]) -> dict[str, Any]:
    """One paired pass over every task for the requested strategies."""
    tasks, _corpus = load_tasks(args.tasks, include_holdout=args.include_holdout)
    api_key = _read_api_key(args.api_key_file) if "brain_first" in strategies else ""
    reports: dict[str, list[dict[str, Any]]] = {}
    if "brain_first" in strategies:
        semaphore = asyncio.Semaphore(args.concurrency)
        async def evaluate(task: TokenEconomyTask) -> dict[str, Any]:
            started = time.perf_counter()
            try:
                async with semaphore:
                    result = await _brain_first(args.api_url, api_key, args.repo_path, args.local_root, task, args.limit, args.targeted_read_max_bytes, args.range_padding, args.timeout)
            except Exception as exc:
                result = {"strategy": "brain_first", "locators": [], "error": type(exc).__name__, "locator_bytes": 0, "read_bytes": 0, "retrieved_bytes": 0, "estimated_tokens": 0, "tool_calls": 1, "latency_ms": round((time.perf_counter() - started) * 1000, 2), "hit_at_3_any": False, "mandatory_files_found": [], "expected_symbols_found": [], "mandatory_ranges_found": [], "completion_assertions_found": [], "literal_assertions_missing": {}, "semantic_assertions_unscored": 0, "evidence_complete": False, "failure_reasons": [f"strategy_error:{type(exc).__name__}"], "terminal_outcome": "error"}
            return {"task": task.to_dict(), "result": result}
        reports["brain_first"] = list(await asyncio.gather(*(evaluate(task) for task in tasks)))
    if "local_rg_read" in strategies:
        reports["local_rg_read"] = [{"task": task.to_dict(), "result": _local_rg_read(task, args.local_root, args.local_limit, args.targeted_read_max_bytes, args.range_padding)} for task in tasks]
    return reports


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    tasks, corpus = load_tasks(args.tasks, include_holdout=args.include_holdout)
    if not corpus["acceptance_eligible"] and not args.allow_non_production_baseline:
        reasons = ", ".join(corpus["acceptance_ineligible_reasons"][:8])
        raise ValueError(f"Corpus is not production-acceptance eligible: {reasons}")
    strategies = [args.strategy] if args.strategy != "both" else ["brain_first", "local_rg_read"]
    identity: dict[str, Any] = {}
    if "brain_first" in strategies:
        try:
            status, identity, _raw = await asyncio.to_thread(_request_json, args.api_url.rstrip("/") + "/api/version", {}, None, args.timeout)
            if not 200 <= status < 300:
                identity = {"unavailable": True, "status": status}
        except Exception as exc:
            identity = {"unavailable": True, "error": type(exc).__name__}
    pin_check = _pin_check(tasks, corpus, args.local_root)
    # Repeat each arm; report every repeat's summary plus per-task rows of the
    # first repeat (the raw artifact keeps all repeats per strategy).
    repeats: list[dict[str, Any]] = []
    strategy_tasks: dict[str, list[dict[str, Any]]] = {}
    for _ in range(max(1, args.repeats)):
        pass_reports = await _run_once(args, strategies)
        repeats.append(
            {strategy: {"summary": _summary(rows)} for strategy, rows in pass_reports.items()}
        )
        for strategy, rows in pass_reports.items():
            strategy_tasks.setdefault(strategy, []).append(rows)
    strategies_report = {
        strategy: {
            "summary": repeats[-1][strategy]["summary"],
            "repeat_summaries": [rep[strategy]["summary"] for rep in repeats],
            "tasks": strategy_tasks[strategy][0],
            "task_repeats": strategy_tasks[strategy],
        }
        for strategy in strategies
    }
    return {
        "schema_version": 2,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "acceptance": {"eligible": corpus["acceptance_eligible"], "reasons": corpus["acceptance_ineligible_reasons"]},
        "corpus": corpus,
        "pin_check": pin_check,
        "provider_usage": {
            "mode": os.environ.get("DEFAULT_LLM_PROVIDER", "mock"),
            "actual_provider_tokens": None,
            "note": "estimated transport tokens only (ceil(utf8_bytes/4)); no provider calls in either arm",
        },
        "config": {"api_url": args.api_url, "repo_path": args.repo_path, "local_root": str(args.local_root), "timeout_seconds": args.timeout, "search_limit": args.limit, "local_result_limit": args.local_limit, "targeted_read_max_bytes": args.targeted_read_max_bytes, "range_padding": args.range_padding, "token_estimator": "ceil(utf8_bytes / 4)", "repeats": max(1, args.repeats)},
        "identity": {"python": sys.version.split()[0], "platform": platform.platform(), "brain": identity},
        "strategies": strategies_report,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--strategy", choices=("both", "brain_first", "local_rg_read"), default="both")
    parser.add_argument("--api-url", default=os.environ.get("BRAIN_API_URL", "http://127.0.0.1:8010"))
    parser.add_argument("--api-key-file", type=Path, default=None)
    parser.add_argument("--repo-path", default="/app")
    parser.add_argument("--local-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--local-limit", type=int, default=5)
    parser.add_argument("--targeted-read-max-bytes", type=int, default=3200)
    parser.add_argument("--range-padding", type=int, default=12)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--concurrency", type=int, default=4, choices=range(1, 11))
    parser.add_argument("--repeats", type=int, default=1, choices=range(1, 21))
    parser.add_argument("--include-holdout", action="store_true", help="score the untouched holdout tasks too (they must not drive tuning)")
    parser.add_argument("--allow-non-production-baseline", action="store_true", help="record historical curated baseline only; never acceptance evidence")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    args.local_root = args.local_root.resolve()
    if not args.local_root.is_dir():
        raise SystemExit(f"Missing local root: {args.local_root}")
    try:
        report = asyncio.run(_run(args))
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for strategy, value in report["strategies"].items():
        summary = value["summary"]
        print(f"{strategy}: evidence_complete={summary['evidence_complete_rate']:.1%} hit@3={summary['hit_at_3_any']:.1%} literal_assertions={summary['literal_assertion_coverage']:.1%} range_recall={summary['mandatory_range_recall']:.1%} median_tokens={summary['median_total_context_tokens']} p95_ms={summary['p95_latency_ms']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
