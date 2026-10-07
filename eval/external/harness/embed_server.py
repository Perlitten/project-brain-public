"""OpenAI-compatible embedding shim over fastembed (local, CPU, ONNX).

Serves jinaai/jina-embeddings-v2-base-code at /v1/embeddings so Project Brain's
``openai_compatible`` embedding provider can talk to a real open model.

- Persistent sha256(text)->vector disk cache: identical chunk contents are
  embedded exactly once across all repos/commits/systems.
- Dynamic batching: sorts each request by length and caps batch size in both
  count and total chars (ONNX pads to batch max; a single 100k-char input
  otherwise inflates attention memory into tens of GB).
- Server-side truncation safety net at EVAL_EMBED_MAX_CHARS (default 30000);
  the prod truncation knob is Brain's EMBEDDING_MAX_INPUT_CHARS (see env.sh).

Protocol: eval/external/PROTOCOL.md section 4.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

MODEL_ID = os.environ.get("EVAL_EMBED_MODEL", "jinaai/jina-embeddings-v2-base-code")
CACHE_PATH = Path(os.environ.get("EVAL_EMBED_CACHE", "/home/ubuntu/eval_external/embed_cache.sqlite"))
PORT = int(os.environ.get("EVAL_EMBED_PORT", "18099"))
DIM = int(os.environ.get("EVAL_EMBED_DIM", "768"))
MAX_CHARS = int(os.environ.get("EVAL_EMBED_MAX_CHARS", "30000"))
BATCH_MAX_ITEMS = int(os.environ.get("EVAL_EMBED_BATCH_ITEMS", "96"))
BATCH_MAX_CHARS = int(os.environ.get("EVAL_EMBED_BATCH_CHARS", str(180_000)))

app = FastAPI()
_model = None
_db: sqlite3.Connection | None = None
_lock = threading.Lock()
_stats = {"requests": 0, "texts": 0, "cache_hits": 0, "embed_calls": 0}


def _cache_get(keys: list[str]) -> dict[str, list[float]]:
    if not keys:
        return {}
    out: dict[str, list[float]] = {}
    for i in range(0, len(keys), 900):
        chunk = keys[i : i + 900]
        q = ",".join("?" * len(chunk))
        for k, v in _db.execute(f"SELECT k, v FROM emb_cache WHERE k IN ({q})", chunk):
            out[k] = json.loads(v)
    return out


def _cache_put(pairs: list[tuple[str, str]]) -> None:
    _db.executemany("INSERT OR REPLACE INTO emb_cache (k, v) VALUES (?, ?)", pairs)
    _db.commit()


def _embed_batched(texts: list[str]) -> list[list[float]]:
    """Length-sorted, size+char-capped batches to bound ONNX memory."""
    order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
    vecs: list[list[float] | None] = [None] * len(texts)
    batch: list[tuple[int, str]] = []
    batch_chars = 0
    groups: list[list[tuple[int, str]]] = []
    for i in order:
        t = texts[i][:MAX_CHARS]
        w = len(t)
        if batch and (len(batch) >= BATCH_MAX_ITEMS or batch_chars + w > BATCH_MAX_CHARS):
            groups.append(batch)
            batch, batch_chars = [], 0
        batch.append((i, t))
        batch_chars += w
    if batch:
        groups.append(batch)
    for g in groups:
        got = list(_model.embed([t for _, t in g]))
        for (i, _), vec in zip(g, got):
            vecs[i] = [float(x) for x in vec]
    return [v or [] for v in vecs]


@app.on_event("startup")
def _startup() -> None:
    global _model, _db
    from fastembed import TextEmbedding

    _model = TextEmbedding(model_name=MODEL_ID)
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _db = sqlite3.connect(str(CACHE_PATH), check_same_thread=False)
    _db.execute("CREATE TABLE IF NOT EXISTS emb_cache (k TEXT PRIMARY KEY, v TEXT)")
    _db.execute("PRAGMA journal_mode=WAL")
    print(f"[embed_server] model={MODEL_ID} dim={DIM} cache={CACHE_PATH}", flush=True)


@app.post("/v1/embeddings")
async def embeddings(req: Request) -> JSONResponse:
    body = await req.json()
    inp = body.get("input", [])
    texts = [inp] if isinstance(inp, str) else list(inp)
    if not texts:
        return JSONResponse({"object": "list", "data": [], "model": MODEL_ID,
                             "usage": {"prompt_tokens": 0, "total_tokens": 0}})
    _stats["requests"] += 1
    _stats["texts"] += len(texts)
    keys = [hashlib.sha256(t[:MAX_CHARS].encode("utf-8", errors="ignore")).hexdigest() for t in texts]
    with _lock:
        cached = _cache_get(keys)
        missing_idx = [i for i, k in enumerate(keys) if k not in cached]
        fresh: dict[str, list[float]] = {}
        if missing_idx and _model is not None:
            to_embed = [texts[i] for i in missing_idx]
            vecs = _embed_batched(to_embed)
            pairs = []
            for i, vec in zip(missing_idx, vecs):
                fresh[keys[i]] = vec
                pairs.append((keys[i], json.dumps(vec)))
            _cache_put(pairs)
            _stats["embed_calls"] += len(missing_idx)
        _stats["cache_hits"] += len(texts) - len(missing_idx)
    data = []
    for i, k in enumerate(keys):
        vec = cached.get(k) or fresh.get(k) or [0.0] * DIM
        data.append({"object": "embedding", "index": i, "embedding": vec})
    toks = sum(len(t.split()) for t in texts)
    return JSONResponse({"object": "list", "data": data, "model": MODEL_ID,
                         "usage": {"prompt_tokens": toks, "total_tokens": toks}})


@app.get("/v1/models")
async def models() -> JSONResponse:
    return JSONResponse({"object": "list", "data": [{"id": MODEL_ID, "object": "model"}]})


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "model": MODEL_ID, "stats": _stats})


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
