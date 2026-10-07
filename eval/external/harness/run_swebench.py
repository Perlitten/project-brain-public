"""Phase 1: SWE-bench file localization against Project Brain + baselines.

Per instance: checkout repo@base_commit -> incremental `brain index` -> run all
system arms -> append one JSONL row. Resumable via --out (already-done ids are
skipped). Protocol: ../PROTOCOL.md + ../PROTOCOL_AMENDMENTS.md.

Usage:
  python run_swebench.py --subset stratified60
  python run_swebench.py --subset all
  python run_swebench.py --instances django__django-11019 --out results/dev.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
import time
import traceback
from collections import defaultdict
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402,F401  (sets env before brain imports)

from common import (  # noqa: E402
    DEV_FOLD,
    REPOS_DIR,
    SEED,
    JsonlWriter,
    commit_timestamp,
    done_ids,
    git,
    grep_baseline,
    parse_gold_patch,
    perf_ms,
    repo_local_name,
    tokenize,
)

from datasets import load_dataset  # noqa: E402
from rank_bm25 import BM25Okapi  # noqa: E402
from sqlalchemy import select  # noqa: E402

from brain.config.settings import settings  # noqa: E402
from brain.context.context_pack_builder import ContextPackBuilder  # noqa: E402
from brain.database.models import File, FileChunk, Symbol  # noqa: E402
from brain.database.repository_utils import get_repository_by_path  # noqa: E402
from brain.database.session import async_session_factory, init_db  # noqa: E402
from brain.indexers.file_indexer import FileIndexer  # noqa: E402
from brain.retrieval.pipeline import HybridRetrievalPipeline  # noqa: E402
import brain.retrieval.pipeline as pipeline_mod  # noqa: E402
from brain.retrieval.service import RetrievalService  # noqa: E402
from brain.search.code_search import VectorSearchResult, extract_keywords  # noqa: E402
from brain.embeddings.constants import VectorSearchStatus  # noqa: E402

FILE_LIMIT = 30
TOP_K = 30

# v2 ablation variants (STEP 2, amendment A6): each entry flips a subset of the
# RETRIEVAL_V2_* flags; RETRIEVAL_V2_ENABLED is toggled on per variant run.
# Tuning decisions are made on dev150 only — never on Lite.
V2_VARIANTS: dict[str, dict] = {
    "brain_v7_full": {},
    "brain_v7_norank": {"RETRIEVAL_V2_RERANK_ENABLED": False},
    "brain_v7_rrf": {"RETRIEVAL_V2_FUSION": "rrf"},
    "brain_v7_lin": {"RETRIEVAL_V2_FUSION": "linear"},
    "brain_v7_sum": {"RETRIEVAL_V2_VEC_AGG": "sum"},
    "brain_v7_topk": {"RETRIEVAL_V2_VEC_AGG": "topk"},
    "brain_v7_noidq": {"RETRIEVAL_V2_ID_QUERY": False},
    "brain_v7_nobm25": {"RETRIEVAL_V2_BM25_FALLBACK": False},
    "brain_v7_wrrf1": {"RETRIEVAL_V2_WRRF_VEC_WEIGHT": 1.0},
    "brain_v7_wrrf4": {"RETRIEVAL_V2_WRRF_VEC_WEIGHT": 4.0},
    # second-round dev cells added after first grid showed linear >> wrrf:
    "brain_v7_lin_norank": {"RETRIEVAL_V2_FUSION": "linear", "RETRIEVAL_V2_RERANK_ENABLED": False},
    "brain_v7_lin_sum": {"RETRIEVAL_V2_FUSION": "linear", "RETRIEVAL_V2_VEC_AGG": "sum"},
    "brain_v7_lin_topk": {"RETRIEVAL_V2_FUSION": "linear", "RETRIEVAL_V2_VEC_AGG": "topk"},
    "brain_v7_lin_noidq": {"RETRIEVAL_V2_FUSION": "linear", "RETRIEVAL_V2_ID_QUERY": False},
    "brain_v7_lin_nobm25": {"RETRIEVAL_V2_FUSION": "linear", "RETRIEVAL_V2_BM25_FALLBACK": False},
    # round-3 cells: is linear just dense in disguise, or do channels help?
    "brain_v7_lin_chan0": {
        "RETRIEVAL_V2_FUSION": "linear",
        "RETRIEVAL_V2_LIN_W_LEX": 0.0, "RETRIEVAL_V2_LIN_W_SYM": 0.0,
        "RETRIEVAL_V2_LIN_W_HINT": 0.0, "RETRIEVAL_V2_LIN_W_GRAPH": 0.0,
    },
    "brain_v7_lin_hilex": {
        "RETRIEVAL_V2_FUSION": "linear",
        "RETRIEVAL_V2_LIN_W_LEX": 0.6, "RETRIEVAL_V2_LIN_W_SYM": 0.6,
        "RETRIEVAL_V2_LIN_W_HINT": 0.6,
    },
}

# STEP 3 frozen config — filled in AFTER dev150 ablation picks winners.
# {} = committed settings defaults; entries here are the dev-chosen overrides
# applied on top of RETRIEVAL_V2_ENABLED=True for the single test-set run.
V2_FROZEN: dict = {}


async def run_pipeline(issue: str, repo_id: int, repo_name: str, task_type: str, keywords: list[str]):
    t0 = time.perf_counter()
    pipe = HybridRetrievalPipeline()
    res = await pipe.run(
        task_description=issue,
        task_type=task_type,
        keywords=keywords,
        repository_id=repo_id,
        repository_name=repo_name,
        file_limit=FILE_LIMIT,
        retrieval_mode="fast",
    )
    return res, perf_ms(t0)


def channel_rank(cands) -> list[str]:
    return [c.item_id for c in cands]


async def brain_arms(issue: str, repo_id: int, repo_name: str, builder: ContextPackBuilder,
                     v2: bool = False, add_v7: bool = False, v1_ablations: bool = True) -> dict:
    out: dict[str, dict] = {}
    task_type, keywords, _risks, _feats = await builder._classify_task(issue)
    meta = {"task_type": task_type, "n_keywords": len(keywords), "keywords": keywords[:60]}

    async def _pack(res, ms):
        by_ch = res.candidates_by_channel
        return {
            "paths": list(res.selected_paths)[:TOP_K],
            "fused": [c.item_id for c in res.fused_ranking][:TOP_K],
            "dense": channel_rank(by_ch.get("vector", []))[:TOP_K],
            "lex": channel_rank(by_ch.get("lexical", []))[:TOP_K],
            "pool": list(res.pool_paths)[:100],
            "ms": ms,
            "route": res.route.value,
            "vector_status": res.vector_status,
            "channel_counts": {k: len(v) for k, v in by_ch.items()},
        }

    # brain-v5 (prod defaults)
    try:
        res, ms = await run_pipeline(issue, repo_id, repo_name, task_type, keywords)
        packed = await _pack(res, ms)
        out["brain_v5"] = {"paths": packed["paths"], "ms": ms}
        out["brain_fused"] = {"paths": packed["fused"], "ms": 0.0}
        out["dense"] = {"paths": packed["dense"], "ms": 0.0}
        out["lex_only_channel"] = {"paths": packed["lex"], "ms": 0.0}
        out["brain_v5_meta"] = {k: packed[k] for k in ("route", "vector_status", "channel_counts")}
        out["_pool"] = packed["pool"]
    except Exception as exc:
        out["brain_v5"] = {"paths": [], "ms": 0.0, "error": f"{type(exc).__name__}: {exc}"}

    if not v2 and v1_ablations:
        # brain-v6 (shipped feature flag on)
        try:
            settings.RETRIEVAL_V6_ENABLED = True
            res, ms = await run_pipeline(issue, repo_id, repo_name, task_type, keywords)
            packed = await _pack(res, ms)
            out["brain_v6"] = {"paths": packed["paths"], "ms": ms}
            out["brain_v6_meta"] = {k: packed[k] for k in ("route", "vector_status", "channel_counts")}
        except Exception as exc:
            out["brain_v6"] = {"paths": [], "ms": 0.0, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            settings.RETRIEVAL_V6_ENABLED = False

        # brain-novector: dense channel patched empty
        try:
            empty = VectorSearchResult(matches=[], status=VectorSearchStatus.OK)
            with mock.patch.object(
                pipeline_mod, "vector_search_chunks",
                new=mock.AsyncMock(return_value=empty),
            ):
                res, ms = await run_pipeline(issue, repo_id, repo_name, task_type, keywords)
            packed = await _pack(res, ms)
            out["brain_novector"] = {"paths": packed["paths"], "ms": ms}
        except Exception as exc:
            out["brain_novector"] = {"paths": [], "ms": 0.0, "error": f"{type(exc).__name__}: {exc}"}

    # brain-nolex: no lexical/symbol/hints signal at all
    if not v2 and v1_ablations:
        try:
            with mock.patch.object(pipeline_mod, "expand_keywords", new=lambda *a, **k: []), \
                 mock.patch.object(pipeline_mod, "derive_path_hints", new=lambda *a, **k: []):
                res, ms = await run_pipeline(issue, repo_id, repo_name, task_type, keywords)
            packed = await _pack(res, ms)
            out["brain_nolex"] = {"paths": packed["paths"], "ms": ms}
        except Exception as exc:
            out["brain_nolex"] = {"paths": [], "ms": 0.0, "error": f"{type(exc).__name__}: {exc}"}

    # brain-v7 variants (v2 ablation grid, dev split only)
    if v2:
        wanted = v2 if isinstance(v2, (list, tuple, set)) else V2_VARIANTS.keys()
        for vname in wanted:
            overrides = V2_VARIANTS[vname]
            saved: dict[str, object] = {}
            try:
                settings.RETRIEVAL_V2_ENABLED = True
                for k, val in overrides.items():
                    saved[k] = getattr(settings, k)
                    setattr(settings, k, val)
                res, ms = await run_pipeline(issue, repo_id, repo_name, task_type, keywords)
                packed = await _pack(res, ms)
                out[vname] = {
                    "paths": packed["paths"],
                    "fused": packed["fused"],
                    "ms": ms,
                    "channel_counts": packed["channel_counts"],
                }
            except Exception as exc:
                out[vname] = {"paths": [], "ms": 0.0, "error": f"{type(exc).__name__}: {exc}"}
            finally:
                settings.RETRIEVAL_V2_ENABLED = False
                for k, val in saved.items():
                    setattr(settings, k, val)

    # brain_v7: single arm at the STEP-3 frozen v2 config (test-set run)
    if add_v7:
        saved = {}
        try:
            settings.RETRIEVAL_V2_ENABLED = True
            for k, val in V2_FROZEN.items():
                saved[k] = getattr(settings, k)
                setattr(settings, k, val)
            res, ms = await run_pipeline(issue, repo_id, repo_name, task_type, keywords)
            packed = await _pack(res, ms)
            out["brain_v7"] = {
                "paths": packed["paths"],
                "fused": packed["fused"],
                "ms": ms,
                "channel_counts": packed["channel_counts"],
            }
        except Exception as exc:
            out["brain_v7"] = {"paths": [], "ms": 0.0, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            settings.RETRIEVAL_V2_ENABLED = False
            for k, val in saved.items():
                setattr(settings, k, val)

    out["query_meta"] = meta
    return out


async def mcp_arm(issue: str, repo_path: str) -> dict:
    try:
        t0 = time.perf_counter()
        svc = RetrievalService()
        result = await svc.retrieve(
            issue, repo_path=repo_path, intent="locator", candidate_budget=TOP_K, deadline_s=10.0
        )
        paths = [c.path for c in result.candidates][:TOP_K]
        return {"paths": paths, "ms": perf_ms(t0), "degraded": result.degraded}
    except Exception as exc:
        return {"paths": [], "ms": 0.0, "error": f"{type(exc).__name__}: {exc}"}


async def bm25_arm(issue: str, repo_id: int) -> dict:
    t0 = time.perf_counter()
    async with async_session_factory() as session:
        rows = (
            await session.execute(
                select(FileChunk.content, File.path)
                .join(File, FileChunk.file_id == File.id)
                .where(File.repository_id == repo_id)
            )
        ).all()
    if not rows:
        return {"paths": [], "ms": 0.0, "n_docs": 0}
    corpus = [tokenize(r[0] or "") for r in rows]
    bm = BM25Okapi(corpus)
    scores = bm.get_scores(tokenize(issue))
    by_path: dict[str, float] = {}
    for (content, path), s in zip(rows, scores):
        if s > by_path.get(path, 0.0):
            by_path[path] = float(s)
    ranked = [p for p, _ in sorted(by_path.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_K]]
    return {"paths": ranked, "ms": perf_ms(t0), "n_docs": len(rows)}


async def gold_symbols(repo_id: int, gold_files: list[str], hunks: dict) -> list[str]:
    """Names of symbols overlapping gold hunks — for symbol-level recall."""
    if not gold_files:
        return []
    async with async_session_factory() as session:
        rows = (
            await session.execute(
                select(Symbol.name, Symbol.start_line, Symbol.end_line, File.path)
                .join(File, Symbol.file_id == File.id)
                .where(File.repository_id == repo_id, File.path.in_(gold_files))
            )
        ).all()
    names = []
    for name, s, e, path in rows:
        for hs, hl in hunks.get(path, []):
            if s is None:
                continue
            if s <= hs + hl and (e or s) >= hs:
                names.append(f"{path}::{name}")
                break
    return names


async def index_repo(repo_dir: Path) -> dict:
    t0 = time.perf_counter()
    indexer = FileIndexer()
    rec = await indexer.index_repository(repo_dir, clean=False)
    return {
        "ms": perf_ms(t0),
        "repo_id": rec.id,
        "repo_name": rec.name,
        "last_commit": rec.last_indexed_commit,
        "file_counts": getattr(indexer, "file_counts", {}),
    }


def neo4j_up() -> bool:
    import socket
    try:
        with socket.create_connection(("localhost", 7687), timeout=1):
            return True
    except OSError:
        return False


def select_instances(ds, subset: str, ids: list[str] | None) -> list[dict]:
    rows = [dict(r) for r in ds]
    if ids:
        # explicit ids bypass the dev-fold exclusion (dev fold exists for smoke tests)
        keep = set(ids)
        return [r for r in rows if r["instance_id"] in keep]
    rows = [r for r in rows if r["instance_id"] not in DEV_FOLD]
    if subset == "all":
        return rows
    if subset == "stratified60":
        rng = random.Random(SEED)
        by_repo: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            by_repo[r["repo"]].append(r)
        total = len(rows)
        picked: list[dict] = []
        for repo, items in sorted(by_repo.items()):
            k = max(2, round(60 * len(items) / total))
            k = min(k, len(items))
            picked.extend(rng.sample(items, k))
        # trim to exactly 60 deterministically
        rng2 = random.Random(SEED + 1)
        rng2.shuffle(picked)
        return picked[:60]
    raise SystemExit(f"unknown subset {subset}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--subset", default="stratified60", choices=["stratified60", "all"])
    ap.add_argument("--instances", nargs="*", default=None)
    ap.add_argument("--ids-file", default=None,
                    help="file with one instance_id per line (e.g. splits/dev150_ids.txt)")
    ap.add_argument("--v2", action="store_true",
                    help="dev tuning run: brain_v5 + brain_v7_* variant arms only "
                         "(skips v6/novector/nolex/mcp/bm25/grep)")
    ap.add_argument("--variants", nargs="*", default=None,
                    help="subset of V2_VARIANTS names to run (implies --v2)")
    ap.add_argument("--v7", action="store_true",
                    help="final run: all v1 arms + brain_v7 at V2_FROZEN config")
    ap.add_argument("--repos", nargs="*", default=None, help="restrict to these repos (sharding)")
    ap.add_argument("--dataset", default="princeton-nlp/SWE-bench_Lite")
    ap.add_argument("--out", default=None)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--index-once", action="store_true",
                    help="index each repo once at its earliest base_commit (amendment A5)")
    args = ap.parse_args()

    out_path = Path(args.out) if args.out else (
        Path(__file__).resolve().parent.parent / "results" / f"swebench_{args.subset}.jsonl"
    )
    seen = done_ids(out_path)
    ids = list(args.instances or [])
    if args.ids_file:
        ids += [
            line.strip()
            for line in Path(args.ids_file).read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]
    ds = load_dataset(args.dataset, split="test")
    insts = select_instances(ds, args.subset, ids or None)
    if args.repos:
        insts = [i for i in insts if i["repo"] in set(args.repos)]
    insts = [i for i in insts if i["instance_id"] not in seen]
    if args.limit:
        insts = insts[: args.limit]
    print(f"[run] {len(insts)} instances -> {out_path}", flush=True)

    await init_db()
    builder = ContextPackBuilder()
    writer = JsonlWriter(out_path)
    graph_up = neo4j_up()
    print(f"[run] neo4j={'up' if graph_up else 'down'}", flush=True)

    by_repo: dict[str, list[dict]] = defaultdict(list)
    for r in insts:
        by_repo[r["repo"]].append(r)

    done = 0
    for repo, items in sorted(by_repo.items()):
        repo_dir = REPOS_DIR / repo_local_name(repo)
        if not repo_dir.exists():
            print(f"[skip] missing clone {repo_dir}", flush=True)
            continue
        items.sort(key=lambda r: commit_timestamp(repo_dir, r["base_commit"]))
        snapshot_idx = None
        if args.index_once:
            rec = await get_repository_by_path(str(repo_dir))
            snap_commit = (rec.last_indexed_commit if rec else None) or items[0]["base_commit"]
            try:
                git(repo_dir, "checkout", "-q", "-f", snap_commit)
            except Exception as exc:
                print(f"[skip] snapshot checkout {repo}: {exc}", flush=True)
                continue
            if rec is None:
                try:
                    snapshot_idx = await index_repo(repo_dir)
                    print(f"[snap] {repo} indexed once at {snap_commit[:8]} "
                          f"({snapshot_idx['ms']}ms)", flush=True)
                except Exception as exc:
                    print(f"[skip] snapshot index {repo}: {exc}", flush=True)
                    continue
            else:
                snapshot_idx = {"ms": 0.0, "repo_id": rec.id, "repo_name": rec.name,
                                "last_commit": rec.last_indexed_commit,
                                "file_counts": "reused"}
                print(f"[snap] {repo} reusing index at "
                      f"{(rec.last_indexed_commit or '?')[:8]}", flush=True)
        for inst in items:
            iid = inst["instance_id"]
            issue = inst["problem_statement"]
            gold_files, hunks = parse_gold_patch(inst["patch"])
            row = {
                "instance_id": iid,
                "repo": repo,
                "base_commit": inst["base_commit"],
                "gold_files": gold_files,
                "gold_hunks": hunks,
                "issue_sha256": __import__("hashlib").sha256(issue.encode()).hexdigest()[:16],
                "graph_available": graph_up,
            }
            if args.index_once:
                idx = snapshot_idx
                # gold file absent from the snapshot index => scored as a natural miss
                row["snapshot_commit"] = idx.get("last_commit") or items[0]["base_commit"]
                row["gold_at_snapshot"] = bool(
                    gold_files and all((repo_dir / gf).exists() for gf in gold_files))
            else:
                try:
                    git(repo_dir, "checkout", "-q", "-f", inst["base_commit"])
                except Exception as exc:
                    row["error"] = f"checkout: {exc}"
                    writer.write(row)
                    continue
                try:
                    idx = await index_repo(repo_dir)
                except Exception as exc:
                    row["error"] = f"index: {type(exc).__name__}: {exc}"
                    row["trace"] = traceback.format_exc()[-2000:]
                    writer.write(row)
                    continue
            row["index"] = idx
            repo_id = idx["repo_id"]

            try:
                v2_arg = args.variants if args.variants else args.v2
                arms = await brain_arms(issue, repo_id, idx["repo_name"], builder,
                                        v2=v2_arg, add_v7=args.v7)
            except Exception as exc:
                arms = {"fatal": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc()[-2000:]}
            pool = arms.pop("_pool", [])
            row["arms"] = arms
            row["brain_v5_pool"] = pool

            if not (args.v2 or args.variants):
                row["arms"]["brain_mcp"] = await mcp_arm(issue, str(repo_dir))
                row["arms"]["bm25"] = await bm25_arm(issue, repo_id)
                t0 = time.perf_counter()
                row["arms"]["grep"] = {
                    "paths": grep_baseline(
                        repo_dir,
                        arms.get("query_meta", {}).get("keywords") or extract_keywords(issue),
                        TOP_K,
                    ),
                    "ms": perf_ms(t0),
                }
            row["gold_symbols"] = await gold_symbols(repo_id, gold_files, hunks)
            writer.write(row)
            done += 1
            print(f"[{done}/{len(insts)}] {iid} index={idx['ms']}ms", flush=True)
    writer.close()
    print("[run] done", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
