"""Phase 2 — CoIR snippet-level retrieval.

Corpus snippets are loaded directly into the eval DB as files+chunks
(no AST/symbol/graph pass — disclosed amendment A3) and materialized to
disk for the grep baseline. Arms: brain_v5 pipeline + channel/ablation
orders + bm25 + dense + grep — identical scoring to Phase 1 but with
graded nDCG@10 / MRR@10 / Recall@10/30 against qrels.

Usage:
  python run_coir.py --dataset CoIR-Retrieval/cosqa --out results/coir_cosqa.jsonl [--limit 200]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
from common import JsonlWriter, done_ids, perf_ms  # noqa: E402

from datasets import load_dataset  # noqa: E402
from sqlalchemy import select  # noqa: E402

from brain.database.models import File, FileChunk, Repository  # noqa: E402
from brain.database.repository_utils import get_repository_by_path  # noqa: E402
from brain.database.session import async_session_factory, init_db  # noqa: E402
from brain.embeddings.store import build_embedding_record  # noqa: E402
from brain.context.context_pack_builder import ContextPackBuilder  # noqa: E402

import run_swebench as swe  # noqa: E402  reuse arms

CORPUS_ROOT = Path("/home/ubuntu/eval_external/coir_corpus")
EMBED_URL = "http://127.0.0.1:18099/v1/embeddings"


def embed_texts(texts: list[str]) -> list[list[float]]:
    import urllib.request
    req = urllib.request.Request(
        EMBED_URL,
        data=json.dumps({"input": texts, "model": "eval"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    resp = json.loads(urllib.request.urlopen(req, timeout=600).read())
    return [d["embedding"] for d in resp["data"]]


async def load_corpus_repo(ds_name: str, corpus) -> tuple[int, Path, dict[str, str]]:
    """Materialize corpus to disk + insert files/chunks/embeddings. Returns
    (repo_id, corpus_dir, corpus_id->relpath)."""
    short = ds_name.split("/")[-1]
    cdir = CORPUS_ROOT / short
    cdir.mkdir(parents=True, exist_ok=True)
    path_map: dict[str, str] = {}

    repo_path = str(cdir)
    repo = await get_repository_by_path(repo_path)
    if repo:
        async with async_session_factory() as session:
            n = (await session.execute(
                select(File.id).join(FileChunk, FileChunk.file_id == File.id)
                .where(File.repository_id == repo.id)
            )).all()
        if len(n) == len(corpus):
            for i, cid in enumerate(corpus["_id"]):
                path_map[cid] = f"s_{i:06d}.py"
            print(f"[coir] repo already loaded: {repo.id} ({len(path_map)} corpus ids)")
            return repo.id, cdir, path_map
        print(f"[coir] partial load ({len(n)}/{len(corpus)}) — reloading", flush=True)
        async with async_session_factory() as session:
            await session.delete(repo)
            await session.commit()

    t0 = time.perf_counter()
    async with async_session_factory() as session:
        repo_obj = Repository(name=f"coir-{short}", path=repo_path, indexing_status="completed")
        session.add(repo_obj)
        await session.flush()
        repo_id = repo_obj.id
        texts = list(corpus["text"])
        ids = list(corpus["_id"])
        for i, cid in enumerate(ids):
            path_map[cid] = f"s_{i:06d}.py"
        B = 512
        for lo in range(0, len(ids), B):
            batch_ids = ids[lo : lo + B]
            batch_texts = texts[lo : lo + B]
            vecs = embed_texts(batch_texts)
            for cid, text, vec in zip(batch_ids, batch_texts, vecs):
                rel = path_map[cid]
                (cdir / rel).write_text(text or "")
                f = File(repository_id=repo_id, path=rel, language="python",
                         file_type="source_code", hash=hashlib.sha256((text or "").encode()).hexdigest()[:32],
                         size_bytes=len(text or ""))
                session.add(f)
                await session.flush()
                emb = build_embedding_record("file_chunk", 0, vec, text or "")
                session.add(emb)
                await session.flush()
                ch = FileChunk(file_id=f.id, chunk_index=0, content=text or "",
                               start_line=1, end_line=(text or "").count("\n") + 1,
                               embedding_id=emb.id)
                session.add(ch)
                await session.flush()
                emb.entity_id = ch.id
            await session.commit()
            if lo % (B * 10) == 0:
                print(f"[coir] {short}: {lo + B}/{len(ids)} loaded", flush=True)
        await session.commit()
    print(f"[coir] {short}: loaded {len(ids)} snippets in {perf_ms(t0)}ms")
    return repo_id, cdir, path_map


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--queries-config", default="queries")
    ap.add_argument("--v7", action="store_true",
                    help="add brain_v7 arm at the STEP-3 frozen v2 config")
    args = ap.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    done = done_ids(out_path)

    corpus = load_dataset(args.dataset, "corpus", split="corpus")
    queries = load_dataset(args.dataset, args.queries_config, split="queries")
    qrels = load_dataset(args.dataset, "default", split="test")

    rel: dict[str, dict[str, int]] = {}
    for r in qrels:
        rel.setdefault(str(r["query-id"]), {})[str(r["corpus-id"])] = int(r["score"])
    qmap = {str(r["_id"]): r["text"] for r in queries}
    qids = [q for q in rel if q in qmap and q not in done]
    if args.limit:
        qids = qids[: args.limit]
    print(f"[coir] {args.dataset}: {len(qids)} test queries ({len(done)} done)", flush=True)

    await init_db()
    repo_id, cdir, path_map = await load_corpus_repo(args.dataset, corpus)
    rev_map = {v: k for k, v in path_map.items()}
    builder = ContextPackBuilder()
    writer = JsonlWriter(out_path)

    for qi, qid in enumerate(qids):
        qtext = qmap[qid]
        row = {"id": qid, "dataset": args.dataset, "qrels": rel[qid], "arms": {}}
        try:
            # v1_ablations=False: frozen #15 rows already carry v6/novector/nolex;
            # the v2 question needs brain_v7 (+packed dense/lex) only.
            arms = await swe.brain_arms(qtext, repo_id, f"coir-{args.dataset.split('/')[-1]}", builder,
                                        add_v7=args.v7, v1_ablations=not args.v7)
        except Exception as exc:
            arms = {"brain_v5": {"paths": [], "error": str(exc)}}
        try:
            arms["bm25"] = await swe.bm25_arm(qtext, repo_id)
        except Exception as exc:
            arms["bm25"] = {"paths": [], "error": str(exc)}
        try:
            g = common.grep_baseline(cdir, qtext)
            arms["grep"] = {"paths": g["paths"], "ms": g["ms"]}
        except Exception as exc:
            arms["grep"] = {"paths": [], "error": str(exc)}

        # map file paths back to corpus ids
        for aname, arm in list(arms.items()):
            if isinstance(arm, dict) and "paths" in arm:
                arm["ids"] = [rev_map.get(p, p) for p in arm["paths"]]
        arms.pop("_pool", None)
        row["arms"] = arms
        writer.write(row)
        if qi % 50 == 0:
            print(f"[coir] q {qi}/{len(qids)}", flush=True)
    print("[coir] done", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
