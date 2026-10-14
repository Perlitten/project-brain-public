"""Capture the 20 unchanged goldens and 11 context/impact tasks from an API.

Run each arm against the same indexed corpus and provider. This tool sends
retrieval requests only; configure free/offline inference before invoking it.
Credentials come from BRAIN_API_KEY and never enter the captured records.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path

import httpx

from eval.run_golden_eval import _evaluate_question, _metrics

ROOT = Path(__file__).resolve().parents[1]


def input_hash() -> str:
    paths = [ROOT / "eval/golden_set.json", *sorted((ROOT / "eval/harness_benchmark/tasks").glob("*.json"))]
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


async def capture(args: argparse.Namespace) -> dict:
    headers = {"X-API-Key": os.environ["BRAIN_API_KEY"]}
    goldens, tasks = [], []
    async with httpx.AsyncClient(base_url=args.api_url.rstrip("/"), headers=headers, timeout=90) as client:
        for item in json.loads((ROOT / "eval/golden_set.json").read_text(encoding="utf-8")):
            response = await client.post("/search", json={"query": item["question"], "repo_path": args.repo, "limit": 10})
            response.raise_for_status()
            row = _evaluate_question(item, response.json(), 10)
            row.pop("response")  # Keep paths/ranks, not source slices or local runtime metadata.
            goldens.append(row)
        for path in sorted((ROOT / "eval/harness_benchmark/tasks").glob("*.json")):
            task = json.loads(path.read_text(encoding="utf-8"))
            kind = task["type"]
            if kind not in {"context", "impact"}:
                continue
            payload = {"repo_path": args.repo}
            payload.update({"task_description": task["task_description"], "persist": False} if kind == "context"
                           else {"change_request": task["change_request"], "fast": True})
            response = await client.post("/" + kind, json=payload)
            response.raise_for_status()
            data = response.json()
            if kind == "context":
                paths = [row["path"] for row in data.get("candidates", []) if row.get("path")]
            else:
                paths = [row["name"] for row in data.get("affected", []) if isinstance(row, dict) and row.get("name")]
                paths += data.get("directly_affected", []) + data.get("indirectly_affected", [])
                paths = [p for p in paths if isinstance(p, str) and "/" in p and p.endswith((".py", ".ts", ".js", ".tsx"))]
            expected = task.get("must_have_files", [])
            rank = next((i for i, p in enumerate(paths, 1) if p in expected), 0)
            adversarial = {"context-007": len(paths) <= 3, "impact-003": len(paths) < 20, "impact-004": len(paths) <= 75}
            tasks.append({"id": task["id"], "type": kind, "input": task, "paths": paths, "expected": expected,
                          "hit1": rank == 1, "hit3": 1 <= rank <= 3, "mrr": 1 / rank if rank else 0,
                          "recall": sum(p in paths for p in expected) / len(expected) if expected else None,
                          "adversarial_pass": adversarial.get(task["id"])})
    return {"phase": args.phase, "provider": args.provider, "corpus_source": args.corpus_source,
            "input_hash": input_hash(), "golden_metrics": _metrics(goldens), "goldens": goldens, "tasks": tasks,
            "limits": "Retrieval comparison; not learned semantic/SWE/coding-benefit evidence"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("api-url", "repo", "provider", "corpus-source", "phase"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.write_text(json.dumps(asyncio.run(capture(args)), indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
