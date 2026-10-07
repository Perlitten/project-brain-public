#!/usr/bin/env python3
"""Run golden set retrieval evaluation against /search and print Hit/MRR metrics."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import textwrap
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GOLDEN_SET_PATH = PROJECT_ROOT / "eval" / "golden_set.json"
# Overridable so this runs anywhere (CI, the server, another machine) rather
# than only on the laptop it was written on.
API_KEY_PATH = Path(
    os.environ.get("BRAIN_API_KEY_FILE") or Path.home() / ".claude" / "harness" / ".brain-api-key"
)
SEARCH_ENDPOINT = os.environ.get("BRAIN_API_URL", "http://127.0.0.1:8010").rstrip("/") + "/search"


def _read_api_key(path: Path) -> str:
    """Read the key from disk (or BRAIN_API_KEY). It is never printed or written
    to the results file — only sent in the X-API-Key header."""
    env_key = os.environ.get("BRAIN_API_KEY")
    if env_key:
        return env_key.strip()
    key = path.read_text(encoding="utf-8").strip()
    if not key:
        raise ValueError(f"API key file is empty: {path}")
    return key


def _normalize_path(value: str) -> str:
    p = value.replace("\\", "/").strip()
    while "//" in p:
        p = p.replace("//", "/")
    if p.startswith("/app/"):
        p = p[5:]
    elif p == "/app":
        p = ""
    p = p.lstrip("/")
    return p


def _safe_text(value: object, width: int) -> str:
    if value is None:
        return ""
    text = str(value).replace("\n", " ")
    return textwrap.shorten(text, width=width, placeholder="...")


def _hit(position: int, k: int) -> bool:
    return 1 <= position <= k


async def _post_search(
    client: httpx.AsyncClient,
    api_key: str,
    query: str,
    limit: int,
) -> tuple[bool, dict | None, str | None]:
    body = {"query": query, "limit": limit, "repo_path": "/app"}
    headers = {"X-API-Key": api_key}
    try:
        response = await client.post(SEARCH_ENDPOINT, json=body, headers=headers, timeout=30)
        response.raise_for_status()
        return True, response.json(), None
    except httpx.TimeoutException as exc:
        return False, None, f"TimeoutError: {exc}"
    except httpx.HTTPStatusError as exc:
        return False, None, f"HTTPStatusError: {exc}"
    except httpx.HTTPError as exc:
        return False, None, f"HTTPError: {exc}"
    except ValueError as exc:
        return False, None, f"JSONDecodeError: {exc}"
    except Exception as exc:
        return False, None, f"Error: {exc}"


def _evaluate_question(question: dict, response: dict, limit: int) -> dict:
    """Score one question on two channels.

    `/search` answers on three channels and `/ask` feeds the model all of them.
    Scoring only `files[]` measures the LEXICAL channel (ILIKE over path and
    summary) and says nothing about the vector channel, which surfaces as
    `chunks[]`. Measured on the 2026-07-26 baseline: files-only Hit@3 was 15%
    while any-channel Hit@3 was 85% — 14 of 20 questions were answered solely
    by the vector channel. Reporting only the first number would send the next
    person optimising the wrong half of the system.
    """
    expected = [_normalize_path(path) for path in question.get("expect_files", []) if isinstance(path, str)]

    # The current compact /search contract is ``results[].path``.  Keep the
    # pre-unification files/chunks channels readable for historical captures,
    # but reject arbitrary payloads instead of silently scoring an empty list.
    if not isinstance(response, dict):
        raise ValueError("search response must be an object")

    def _paths(key: str, path_key: str) -> list[str]:
        values = response.get(key)
        if values is None:
            return []
        if not isinstance(values, list) or any(not isinstance(item, dict) for item in values):
            raise ValueError(f"search response field {key!r} must be a list of objects")
        paths = []
        for item in values:
            value = item.get(path_key)
            if not isinstance(value, str):
                raise ValueError(f"search response {key} item missing string {path_key!r}")
            paths.append(_normalize_path(value))
        return paths

    has_unified = "results" in response
    has_legacy = "files" in response or "chunks" in response
    if not has_unified and not has_legacy:
        raise ValueError("unknown search response shape: expected results or files/chunks")
    result_paths = _paths("results", "path") if has_unified else []
    file_paths = _paths("files", "path") if "files" in response else []
    chunk_paths = _paths("chunks", "file_path") if "chunks" in response else []
    if has_unified:
        # Unified results are the canonical ranking.  Do not double-count a
        # compatibility channel if a server includes both representations.
        file_paths = result_paths

    def _matches(candidate: str) -> bool:
        return any(
            candidate == expected_path
            or candidate.endswith(f"/{expected_path}")
            or expected_path.endswith(f"/{candidate}")
            for expected_path in expected
        )

    def _rank(paths: list[str]) -> int:
        if not expected:
            return 0
        for idx, path in enumerate(paths, start=1):
            if path and _matches(path):
                return idx
        return 0

    # Each channel is judged on its own top-N, because the prompt shows both
    # lists side by side — a file the vector channel ranks first is right there
    # in "Relevant Code Chunks" regardless of how long the file list is.
    # (Concatenating the channels instead would push every chunk hit past
    # position len(files), making a chunk-only hit unrepresentable at k=3.)
    rank = _rank(file_paths)
    rank_chunks = _rank(chunk_paths)
    rank_any = min([r for r in (rank, rank_chunks) if r] or [0])

    # For the unified contract there is one ranked list.  For legacy captures
    # retain the useful any-channel score because /ask exposes both channels.
    hit1 = _hit(rank_any, 1)
    hit3 = _hit(rank_any, min(3, limit))
    hit5 = _hit(rank_any, min(5, limit))
    mrr = 1.0 / rank_any if rank_any else 0.0
    status = "PASS" if hit3 else "FAIL"

    return {
        "id": question.get("id"),
        "question": question.get("question"),
        "rank": rank,
        "rank_chunks": rank_chunks,
        "rank_any": rank_any,
        "hit_at_1": hit1,
        "hit_at_3": hit3,
        "hit_at_5": hit5,
        "hit_at_3_chunks": _hit(rank_chunks, 3),
        "hit_at_3_any": _hit(rank, 3) or _hit(rank_chunks, 3),
        "mrr": mrr,
        "status": status,
        "expected_files": question.get("expect_files", []),
        "response_files": file_paths,
        "response_chunk_files": chunk_paths,
        "response": response,
        "error": None,
    }


def _metrics(values: list[dict]) -> dict:
    total = len(values)
    if total == 0:
        return {
            "hit_1_count": 0,
            "hit_3_count": 0,
            "hit_5_count": 0,
            "hit_1_pct": 0.0,
            "hit_3_pct": 0.0,
            "hit_5_pct": 0.0,
            "mrr": 0.0,
        }
    hit1_count = sum(1 for value in values if value["hit_at_1"])
    hit3_count = sum(1 for value in values if value["hit_at_3"])
    hit5_count = sum(1 for value in values if value["hit_at_5"])
    hit3_any_count = sum(1 for value in values if value.get("hit_at_3_any"))
    mrr = sum(value["mrr"] for value in values) / total
    return {
        "hit_1_count": hit1_count,
        "hit_3_count": hit3_count,
        "hit_5_count": hit5_count,
        "hit_3_any_count": hit3_any_count,
        "hit_1_pct": round((hit1_count / total) * 100, 2),
        "hit_3_pct": round((hit3_count / total) * 100, 2),
        "hit_5_pct": round((hit5_count / total) * 100, 2),
        "hit_3_any_pct": round((hit3_any_count / total) * 100, 2),
        "mrr": round(mrr, 4),
    }


def _percent(value: float) -> str:
    return f"{value:.2f}%"


async def _evaluate(golden_set: list[dict], api_key: str, limit: int) -> list[dict]:
    async with httpx.AsyncClient(follow_redirects=True) as client:
        results = []
        for question in golden_set:
            qid = question.get("id", "N/A")
            qtext = question.get("question", "")
            error: str | None
            ok, payload, error = await _post_search(client, api_key, qtext, limit)
            if not ok or payload is None:
                expected = question.get("expect_files", [])
                result = {
                    "id": qid,
                    "question": qtext,
                    "rank": 0,
                    "hit_at_1": False,
                    "hit_at_3": False,
                    "hit_at_5": False,
                    "mrr": 0.0,
                    "status": "FAIL",
                    "expected_files": expected,
                    "response_files": [],
                    "response": payload,
                    "error": {
                        "type": str(error).split(":")[0] if error else "Unknown",
                        "message": str(error) if error else "Unknown error",
                    },
                }
                results.append(result)
                continue

            try:
                result = _evaluate_question(question, payload, limit)
            except Exception as exc:
                result = {
                    "id": qid,
                    "question": qtext,
                    "rank": 0,
                    "hit_at_1": False,
                    "hit_at_3": False,
                    "hit_at_5": False,
                    "mrr": 0.0,
                    "status": "FAIL",
                    "expected_files": question.get("expect_files", []),
                    "response_files": [],
                    "response": payload,
                    "error": {"type": "EvaluationError", "message": str(exc)},
                }
            results.append(result)

        return results


def _print_table(results: list[dict]) -> None:
    headers = ["Q", "Question", "Hit@1", "Hit@3", "Hit@5", "MRR", "Status"]
    rows = []
    q_width = max(len(headers[0]), 6)
    question_width = max(len(headers[1]), 24)
    others = [5, 5, 5, 7, 6]
    for idx, result in enumerate(results, start=1):
        q = result["id"] or f"q{idx:03d}"
        q_width = max(q_width, len(str(q)))
        question_width = max(question_width, len(_safe_text(result.get("question"), 72)))
        rows.append(
            (
                str(q),
                _safe_text(result.get("question"), 72),
                "PASS" if result["hit_at_1"] else "FAIL",
                "PASS" if result["hit_at_3"] else "FAIL",
                "PASS" if result["hit_at_5"] else "FAIL",
                f"{result['mrr']:.4f}",
                result["status"],
            )
        )
    header_line = (
        f"{headers[0]:<{q_width}} | {headers[1]:<{question_width}} | "
        f"{headers[2]:<{others[0]}} | {headers[3]:<{others[1]}} | "
        f"{headers[4]:<{others[2]}} | {headers[5]:<{others[3]}} | {headers[6]:<{others[4]}}"
    )
    sep_line = (
        f"{'-' * q_width} | {'-' * question_width} | {'-' * others[0]} | "
        f"{'-' * others[1]} | {'-' * others[2]} | {'-' * others[3]} | {'-' * others[4]}"
    )
    print(header_line)
    print(sep_line)
    for row in rows:
        print(
            f"{row[0]:<{q_width}} | {row[1]:<{question_width}} | "
            f"{row[2]:<{others[0]}} | {row[3]:<{others[1]}} | "
            f"{row[4]:<{others[2]}} | {row[5]:<{others[3]}} | {row[6]:<{others[4]}}"
        )


def _print_summary(metrics: dict) -> None:
    print(
        f"Summary: HIT@1={_percent(metrics['hit_1_pct'])}, "
        f"HIT@3={_percent(metrics['hit_3_pct'])}, "
        f"HIT@5={_percent(metrics['hit_5_pct'])}, "
        f"MRR={metrics['mrr']:.4f}"
    )
    print(f"HIT@3={_percent(metrics['hit_3_pct'])}  (lexical files channel)")
    print(f"HIT@3_ANY={_percent(metrics['hit_3_any_pct'])}  (files + vector chunk channel — what /ask actually sees)")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run golden set eval against /search")
    parser.add_argument("--json", type=Path, default=None, help="Write full results to JSON path")
    parser.add_argument("--threshold", type=float, default=70.0, help="Threshold for HIT@3 in percent")
    parser.add_argument("--limit", type=int, default=5, help="Search limit passed to /search")
    parser.add_argument(
        "--full",
        action="store_true",
        help="Keep each raw /search response in the JSON dump (large: ~700KB/run). "
        "Off by default so saved runs stay small enough to track as a trend.",
    )
    return parser.parse_args()


async def main() -> int:
    args = _parse_args()
    api_key = _read_api_key(API_KEY_PATH)
    golden_set = json.loads(GOLDEN_SET_PATH.read_text(encoding="utf-8"))
    if not isinstance(golden_set, list):
        raise TypeError(f"Expected list in {GOLDEN_SET_PATH}, got {type(golden_set)}")

    results = await _evaluate(golden_set, api_key, args.limit)
    metrics = _metrics(results)

    _print_table(results)
    _print_summary(metrics)

    if args.json:
        saved = results if args.full else [{k: v for k, v in r.items() if k != "response"} for r in results]
        payload = {
            "endpoint": SEARCH_ENDPOINT,
            "limit": args.limit,
            "threshold": args.threshold,
            "count": len(results),
            "summary": {
                "hit@1_pct": metrics["hit_1_pct"],
                "hit@3_pct": metrics["hit_3_pct"],
                "hit@5_pct": metrics["hit_5_pct"],
                "hit@3_any_pct": metrics["hit_3_any_pct"],
                "mrr": metrics["mrr"],
            },
            "results": saved,
        }
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    return 0 if metrics["hit_3_pct"] >= args.threshold else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
