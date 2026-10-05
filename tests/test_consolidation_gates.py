"""Tests for the L2 consolidation pipeline gates (G1–G4)."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from brain.memory.consolidation import (
    ConsolidationCandidate,
    GateOutcome,
    _cosine,
    distill_candidate,
    evaluate_gates,
    run_consolidation,
)


def _event(event_id=1, task_id="t1", classification="learning", summary="dim is 2048"):
    return SimpleNamespace(
        id=event_id,
        task_id=task_id,
        event_type="note",
        classification=classification,
        payload_json={"summary": summary},
        created_at=None,
    )


def _orthogonal_embedder():
    """Embedder returning near-orthogonal vectors for distinct texts."""

    async def embed(text, **kwargs):
        # Deterministic pseudo-vector from text hash: similar texts collide.
        h = abs(hash(text)) % 1000
        return [1.0 if i == h % 16 else 0.0 for i in range(16)]

    async def embed_batch(texts, **kwargs):
        return [await embed(t) for t in texts]

    return SimpleNamespace(embed=embed, embed_batch=embed_batch)


def test_cosine_identical_vectors():
    assert _cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)


def test_cosine_orthogonal_vectors():
    assert _cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_zero_vector():
    assert _cosine([0.0, 0.0], [1.0, 0.0]) == 0.0


@pytest.mark.asyncio
async def test_gate_g2_rejects_single_episode():
    candidate = ConsolidationCandidate(
        statement="Something observed once",
        evidence=[{"event_id": 1}],
        severity="low",
    )
    with patch("brain.memory.consolidation.LearningStore.list_active_learnings", new=AsyncMock(return_value=[])):
        result = await evaluate_gates(candidate)
    assert result.outcome == GateOutcome.REJECT_EVIDENCE
    assert any("G2" in r for r in result.reasons)


@pytest.mark.asyncio
async def test_gate_g1_detects_duplicate():
    existing = SimpleNamespace(id=7, statement="Embeddings dimension is 2048")
    candidate = ConsolidationCandidate(
        statement="Embeddings dimension is 2048",
        evidence=[{"event_id": 1}, {"event_id": 2}],
        severity="low",
    )
    with patch(
        "brain.memory.consolidation.LearningStore.list_active_learnings",
        new=AsyncMock(return_value=[existing]),
    ), patch(
        "brain.memory.consolidation.get_embedding_provider",
        return_value=_orthogonal_embedder(),
    ):
        result = await evaluate_gates(candidate)
    assert result.outcome == GateOutcome.LINK_DUPLICATE
    assert result.duplicate_of == 7


@pytest.mark.asyncio
async def test_gate_g4_requires_approval_for_high_severity():
    candidate = ConsolidationCandidate(
        statement="Drop the production database to fix latency",
        evidence=[{"event_id": 1}, {"event_id": 2}],
        severity="high",
    )

    async def fake_summarize(text, **kwargs):
        return json.dumps({"contradicts": False, "reason": ""})

    fake_summarizer = SimpleNamespace(summarize=fake_summarize)
    with patch(
        "brain.memory.consolidation.LearningStore.list_active_learnings",
        new=AsyncMock(return_value=[]),
    ), patch(
        "brain.memory.consolidation.get_summarizer_provider", return_value=fake_summarizer
    ):
        result = await evaluate_gates(candidate)
    assert result.outcome == GateOutcome.NEEDS_APPROVAL


@pytest.mark.asyncio
async def test_gate_g3_rejects_contradiction():
    existing = SimpleNamespace(id=3, statement="Always use port 5432 for postgres")
    candidate = ConsolidationCandidate(
        statement="Never use port 5432 for postgres",
        evidence=[{"event_id": 1}, {"event_id": 2}],
        severity="low",
    )

    async def fake_summarize(text, **kwargs):
        return json.dumps({"contradicts": True, "reason": "direct conflict on port"})

    fake_summarizer = SimpleNamespace(summarize=fake_summarize)
    with patch(
        "brain.memory.consolidation.LearningStore.list_active_learnings",
        new=AsyncMock(return_value=[existing]),
    ), patch(
        "brain.memory.consolidation.get_embedding_provider",
        return_value=_orthogonal_embedder(),
    ), patch(
        "brain.memory.consolidation.get_summarizer_provider", return_value=fake_summarizer
    ):
        result = await evaluate_gates(candidate)
    # G1 uses orthogonal embedder: distinct statements -> distinct vectors -> pass.
    assert result.outcome == GateOutcome.REJECT_CONTRADICTION


@pytest.mark.asyncio
async def test_distill_candidate_parses_llm_json():
    cluster = [_event(summary="nvidia embeddings dim 2048"), _event(event_id=2, summary="dim confirmed 2048")]

    async def fake_summarize(text, **kwargs):
        assert "nvidia embeddings dim 2048" in text
        return json.dumps({
            "statement": "NVIDIA embeddings dimension is 2048",
            "category": "infra",
            "confidence": 0.9,
            "severity": "low",
        })

    with patch(
        "brain.memory.consolidation.get_summarizer_provider",
        return_value=SimpleNamespace(summarize=fake_summarize),
    ):
        candidate = await distill_candidate(cluster)
    assert candidate.statement == "NVIDIA embeddings dimension is 2048"
    assert candidate.category == "infra"
    assert len(candidate.evidence) == 2


@pytest.mark.asyncio
async def test_distill_candidate_survives_garbage_llm_output():
    cluster = [_event()]

    async def fake_summarize(text, **kwargs):
        return "not json at all"

    with patch(
        "brain.memory.consolidation.get_summarizer_provider",
        return_value=SimpleNamespace(summarize=fake_summarize),
    ):
        candidate = await distill_candidate(cluster)
    # Falls back to raw episode text rather than crashing.
    assert candidate.statement


@pytest.mark.asyncio
async def test_run_consolidation_empty_episodes():
    with patch(
        "brain.memory.consolidation.collect_episodic", new=AsyncMock(return_value=[])
    ):
        report = await run_consolidation()
    assert report["episodes"] == 0
    assert report["candidates"] == 0
    assert report["promoted"] == []
