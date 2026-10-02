"""Compact and score ColBERT token matrices."""

from __future__ import annotations

import numpy as np


class LateInteractionCodecError(ValueError):
    """Stored matrix metadata and bytes disagree."""


def encode_matrix(matrix: np.ndarray, *, dtype: str = "float16") -> bytes:
    values = np.asarray(matrix, dtype=np.float32)
    if values.ndim != 2 or values.shape[0] <= 0 or values.shape[1] <= 0:
        raise LateInteractionCodecError("late-interaction matrix must be non-empty and two-dimensional")
    if not np.isfinite(values).all():
        raise LateInteractionCodecError("late-interaction matrix contains non-finite values")
    if dtype != "float16":
        raise LateInteractionCodecError(f"unsupported late-interaction storage dtype: {dtype}")
    return values.astype("<f2", copy=False).tobytes(order="C")


def decode_matrix(payload: bytes, *, token_count: int, dimension: int, dtype: str = "float16") -> np.ndarray:
    if dtype != "float16":
        raise LateInteractionCodecError(f"unsupported late-interaction storage dtype: {dtype}")
    if token_count <= 0 or dimension <= 0:
        raise LateInteractionCodecError("token_count and dimension must be positive")
    expected = token_count * dimension * np.dtype("<f2").itemsize
    if len(payload) != expected:
        raise LateInteractionCodecError(
            f"late-interaction payload is {len(payload)} bytes; expected {expected} "
            f"for {token_count}x{dimension} {dtype}"
        )
    return np.frombuffer(payload, dtype="<f2").reshape(token_count, dimension).astype(np.float32)


def maxsim_score(query: np.ndarray, document: np.ndarray) -> float:
    """ColBERT MaxSim: sum over query tokens of best document-token dot product."""
    q = np.asarray(query, dtype=np.float32)
    d = np.asarray(document, dtype=np.float32)
    if q.ndim != 2 or d.ndim != 2 or q.shape[1] != d.shape[1]:
        raise LateInteractionCodecError(
            f"incompatible MaxSim shapes: query={q.shape}, document={d.shape}"
        )
    if q.shape[0] == 0 or d.shape[0] == 0:
        raise LateInteractionCodecError("MaxSim matrices must contain at least one token")
    return float((q @ d.T).max(axis=1).sum())
