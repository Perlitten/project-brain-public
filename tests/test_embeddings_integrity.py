"""Tests for embedding integrity helpers."""

import pytest

from brain.embeddings.config import content_hash, get_embedding_config, resolve_embedding_dimension
from brain.embeddings.store import EmbeddingDimensionError, build_embedding_record, validate_vector


def test_resolve_embedding_dimension_mock_default():
    dim = resolve_embedding_dimension("mock")
    assert dim == 1536


def test_content_hash_stable():
    assert content_hash("hello") == content_hash("hello")
    assert content_hash("hello") != content_hash("world")


def test_validate_vector_rejects_wrong_dimension():
    cfg = get_embedding_config("mock")
    with pytest.raises(EmbeddingDimensionError):
        validate_vector([0.1] * (cfg.dimension + 1), cfg)


def test_build_embedding_record_includes_metadata():
    cfg = get_embedding_config("mock")
    vector = [0.0] * cfg.dimension
    vector[0] = 1.0
    emb = build_embedding_record("file_chunk", 1, vector, "print('x')", cfg)
    assert emb.provider == cfg.provider
    assert emb.model == cfg.model
    assert emb.dimension == cfg.dimension
    assert emb.content_hash == content_hash("print('x')")
    assert emb.embedding == vector
