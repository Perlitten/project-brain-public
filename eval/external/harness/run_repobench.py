"""Phase 2b — RepoBench-R (python_cff/python_cfr, gzipped pickles).

Each item: query = code + import_statement; rank `context` candidates;
gold = golden_snippet_index. Per-instance repos cannot be indexed, so
arms are bm25 / dense (same embedder) / brain-rrf (Brain's own RRF
fusion w/(60+rank) with lexical=1.25, vector=1.0 from
brain/search/filters.py:RRF_PIPELINE_WEIGHTS).

Metric: Acc@k — fraction of items whose gold snippet lands in top-k.
"""

from __future__ import annotations

import argparse
import gzip
import pickle
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
from common import JsonlWriter, done_ids, tokenize, perf_ms  # noqa: E402

import numpy as np  # noqa: E402
from datasets.utils.logging import disable_progress_bar  # noqa: E402
from huggingface_hub import hf_hub_download  # noqa: E402
from rank_bm25 import BM25Okapi  # noqa: E402

from run_coir import embed_texts  # noqa: E402

RRF_K = 60.0
RRF_LEX_W = 1.25
RRF_VEC_W = 1.0
SAMPLE = 1500
SEED = 20261006


def load_items(split_file: str) -> list[dict]:
    p = hf_hub_download("tianyang/repobench-r", split_file, repo_type="dataset")
    with gzip.open(p, "rb") as f:
        data = pickle.load(f)
    return data["test"]["easy"] + data["test"]["hard"]


def rank(scores: list[float]) -> list[int]:
    return [i for i, _ in sorted(enumerate(scores), key=lambda kv: (-kv[1], kv[0]))]


def rrf_fuse(rank_a: list[int], rank_b: list[int], wa: float, wb: float) -> list[int]:
    pos_a = {c: i + 1 for i, c in enumerate(rank_a)}
    pos_b = {c: i + 1 for i, c in enumerate(rank_b)}
    ids = set(pos_a) | set(pos_b)
    return sorted(ids, key=lambda c: -(wa / (RRF_K + pos_a.get(c, 10**6)) + wb / (RRF_K + pos_b.get(c, 10**6))))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="data/python_cff.gz")
    ap.add_argument("--out", required=True)
    ap.add_argument("--sample", type=int, default=SAMPLE)
    args = ap.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    done = done_ids(out_path)

    items = load_items(args.split)
    import random
    rng = random.Random(SEED)
    rng.shuffle(items)
    items = [(f"{args.split}:{i}", it) for i, it in enumerate(items[: args.sample])]
    items = [(iid, it) for iid, it in items if iid not in done]
    print(f"[repobench] {len(items)} items -> {out_path}", flush=True)
    writer = JsonlWriter(out_path)

    # Bulk pre-embed every query+candidate once — one HTTP pass over the shim
    # instead of per-item calls that queue behind index batches.
    all_texts: list[str] = []
    spans: list[tuple[int, int]] = []  # (start, n) into all_texts: [query]+cands
    for item_id, it in items:
        cands = it["context"] or []
        query = (it.get("code", "") + "\n" + it.get("import_statement", ""))[-12000:]
        all_texts.append(query)
        all_texts.extend(cands)
        spans.append((len(all_texts) - 1 - len(cands), len(cands) + 1))
    print(f"[repobench] embedding {len(all_texts)} texts", flush=True)
    vecs_all: list[list[float]] = []
    B = 256
    t0 = time.perf_counter()
    for lo in range(0, len(all_texts), B):
        vecs_all.extend(embed_texts(all_texts[lo : lo + B]))
        if lo % (B * 20) == 0:
            print(f"[repobench] emb {lo}/{len(all_texts)}", flush=True)
    emb_ms = perf_ms(t0)
    V = np.array(vecs_all)

    for n, ((item_id, it), (s0, cnt)) in enumerate(zip(items, spans)):
        cands = it["context"]
        gold = int(it["golden_snippet_index"])
        query = (it.get("code", "") + "\n" + it.get("import_statement", ""))[-12000:]
        if not cands:
            continue
        row = {"id": item_id, "n_candidates": len(cands), "gold_index": gold,
               "repo_name": it.get("repo_name", ""), "file_path": it.get("file_path", ""), "arms": {}}
        t1 = time.perf_counter()
        corpus = [tokenize(c) for c in cands]
        bm = BM25Okapi(corpus)
        bm_scores = bm.get_scores(tokenize(query))
        row["arms"]["bm25"] = {"order": rank(list(bm_scores)), "ms": perf_ms(t1)}

        vecs = V[s0 : s0 + cnt]
        qv, cv = vecs[0], vecs[1:]
        denom = (np.linalg.norm(cv, axis=1) * np.linalg.norm(qv) + 1e-9)
        d_scores = (cv @ qv) / denom
        row["arms"]["dense"] = {"order": rank(list(d_scores)), "ms": round(emb_ms / max(len(items), 1), 1)}

        row["arms"]["brain_rrf"] = {"order": rrf_fuse(
            row["arms"]["bm25"]["order"], row["arms"]["dense"]["order"], RRF_LEX_W, RRF_VEC_W)}
        writer.write(row)
        if n % 200 == 0:
            print(f"[repobench] {n}/{len(items)}", flush=True)
    print("[repobench] done", flush=True)


if __name__ == "__main__":
    main()
