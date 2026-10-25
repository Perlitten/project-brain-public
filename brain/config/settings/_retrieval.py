"""Retrieval/ranking fields."""
from __future__ import annotations

from typing import Optional


class SettingsRetrieval:
    # Deterministic retrieval: skip LLM for task classification/keyword extraction
    RETRIEVAL_USE_LLM_CLASSIFICATION: bool = False

    # Optional LLM rerank for top-30 -> top-10 (deterministic fallback always available)
    RETRIEVAL_USE_LLM_RERANK: bool = False

    # v6 P2 two-stage semantic rerank (top-30 -> top-10 rescore). Gated within v6 so the
    # P1-conservative vs P2-semantic selection can be A/B'd; no effect when v6 is off.
    RETRIEVAL_V6_SEMANTIC_RERANK: bool = True

    # v6 P3 file cards: deterministic per-file "card" + card_vector retrieval channel.
    # Off by default; requires RETRIEVAL_V6_ENABLED. v6-off stays frozen v5.
    RETRIEVAL_V6_FILE_CARDS_ENABLED: bool = False

    # Ablation switches (only meaningful when file cards enabled).
    RETRIEVAL_V6_CARD_RECALL: bool = True

    RETRIEVAL_V6_CARD_RERANK: bool = True

    # Default card text format: "code_shaped" | "prose". Chosen by the P3 micro-test -
    # prose surfaced required files better on nv-embedcode (mean card rank 3.5 vs 5.05).
    RETRIEVAL_CARD_TEXT_FORMAT: str = "prose"

    # Moderate card_vector channel weight in fusion (never card-first).
    RETRIEVAL_CARD_VECTOR_WEIGHT: float = 0.9

    # Card embedding provider (pluggable; "" = inherit DEFAULT_EMBEDDING_PROVIDER).
    # Architecture supports a second provider for P3.5 A/B without code changes.
    RETRIEVAL_CARD_EMBEDDING_PROVIDER: Optional[str] = None

    # v6 P2 two-stage rerank: intermediate prune depth (top-100 -> top-30 -> top-10)
    RETRIEVAL_RERANK_PRUNE_LIMIT: int = 30

    # Hard timeout (s) for the optional LLM rerank stage before deterministic fallback
    RETRIEVAL_RERANK_TIMEOUT_S: float = 20.0

    # Retrieval v6 (P0): surface-aware candidate generation + top-100 internal pool.
    # When False the pipeline behaves byte-identically to the frozen Ranking v5 baseline.
    RETRIEVAL_V6_ENABLED: bool = False

    # Internal recall-stage pool depth fed into deterministic rerank (v6 widen).
    RETRIEVAL_POOL_LIMIT: int = 100

    # Precision pack depth - pack output stays at 10 regardless of pool depth.
    RETRIEVAL_PACK_LIMIT: int = 10

    # Minimum candidates each routed surface keeps in the recall pool before global fill.
    RETRIEVAL_SURFACE_MIN_QUOTA: int = 3

    # Retrieval v2 (external-eval driven): dense-primary fusion, gated reranker,
    # empty-channel BM25 fallback, identifier query extraction, vector chunk->file
    # aggregation mode. All legacy defaults — pipeline is byte-identical to v5
    # unless RETRIEVAL_V2_ENABLED is set.
    RETRIEVAL_V2_ENABLED: bool = True

    # Fusion formula: "rrf" (prod), "wrrf" (weighted RRF, vector weight amplified),
    # "linear" (score-normalized weighted sum; empty channels contribute 0).
    # Frozen on dev150 (A7): linear — wrrf still dilutes the vector channel.
    RETRIEVAL_V2_FUSION: str = "linear"

    # Deterministic rerank stage + hard-coded path bonuses; off = select from fusion.
    # Frozen on dev150 (A7): off — removing it was +.159 R@10 excl-0 under wrrf.
    RETRIEVAL_V2_RERANK_ENABLED: bool = False

    # When lexical/symbol/hints/graph/memory all return nothing, compute a
    # content-BM25 channel over chunk text and fuse it (the anonymous-corpus case).
    RETRIEVAL_V2_BM25_FALLBACK: bool = True

    # Extract code identifiers / file paths / stack frames / error strings from
    # the task text and feed them to the lexical/symbol/hints channels.
    RETRIEVAL_V2_ID_QUERY: bool = True

    # Chunk->file aggregation for the vector channel: "max" | "sum" | "topk".
    RETRIEVAL_V2_VEC_AGG: str = "max"

    # wrrf: multiplies the vector channel's RRF weight.
    RETRIEVAL_V2_WRRF_VEC_WEIGHT: float = 2.0

    # linear fusion channel weights (normalized-score weighted sum).
    RETRIEVAL_V2_LIN_W_VEC: float = 1.0

    RETRIEVAL_V2_LIN_W_LEX: float = 0.35

    RETRIEVAL_V2_LIN_W_SYM: float = 0.35

    RETRIEVAL_V2_LIN_W_HINT: float = 0.45

    RETRIEVAL_V2_LIN_W_GRAPH: float = 0.3

    RETRIEVAL_V2_LIN_W_BM25: float = 0.6

    # Per-dependency timeouts (seconds) for retrieval pipeline
    RETRIEVAL_TIMEOUT_VECTOR_S: float = 10.0

    RETRIEVAL_TIMEOUT_NEO4J_S: float = 8.0

    RETRIEVAL_TIMEOUT_LEXICAL_S: float = 5.0
