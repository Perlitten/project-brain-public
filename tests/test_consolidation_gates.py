"""Tests for the L2 consolidation pipeline gates (G1–G4)."""

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from brain.memory.consolidation import (
    EVIDENCE_TIER_UNVALIDATED,
    EVIDENCE_TIER_VALIDATED,
    ConsolidationCandidate,
    GateOutcome,
    _cosine,
    distill_candidate,
    evaluate_gates,
    evidence_tier_for,
    run_consolidation,
)


def _event(event_id=1, task_id="t1", classification="learning", summary="dim is 2048", validated=True,
           author=None):
    payload = {"summary": summary, "reported_validation": validated}
    if author is not None:
        payload["author_principal_id"] = author
    return SimpleNamespace(
        id=event_id,
        task_id=task_id,
        event_type="note",
        classification=classification,
        payload_json=payload,
        created_at=None,
    )


def _gate_patches(active, summarize=None, embedder=None):
    """Common evaluate_gates patch set: active learnings, embedder, summarizer."""
    if summarize is None:
        async def summarize(text, **kwargs):
            return json.dumps({"contradicts": False, "reason": ""})
    return (
        patch("brain.memory.consolidation.LearningStore.list_active_learnings", new=AsyncMock(return_value=active)),
        patch("brain.memory.consolidation.get_embedding_provider", return_value=embedder or _orthogonal_embedder()),
        patch("brain.memory.consolidation.get_summarizer_provider", return_value=SimpleNamespace(summarize=summarize)),
    )


def _orthogonal_embedder():
    """Embedder returning near-orthogonal vectors for distinct texts."""

    async def embed(text, **kwargs):
        # Deterministic pseudo-vector from a stable digest: blake2b is the
        # same in every process, unlike hash() which varies with
        # PYTHONHASHSEED and made this test fail on ~1/8 of CI runs.
        h = int.from_bytes(hashlib.blake2b(text.encode("utf-8"), digest_size=8).digest(), "big") % 1000
        return [1.0 if i == h % 16 else 0.0 for i in range(16)]

    async def embed_batch(texts, **kwargs):
        return [await embed(t) for t in texts]

    return SimpleNamespace(embed=embed, embed_batch=embed_batch)


def _similarity_embedder(similarities: dict):
    """Mock embedder with controllable cosine similarities.

    Each text maps to a 2D unit vector chosen so that the cosine similarity
    between the candidate's vector and a stored learning's vector equals
    the configured value. This simulates real embedding behavior where
    paraphrases score 0.85-0.95 and unrelated texts score near 0 —
    unlike the one-hot _orthogonal_embedder, which can only produce
    similarity 1.0 or 0.0 and therefore cannot test the dedup threshold.
    """
    import math

    async def embed(text, **kwargs):
        s = similarities.get(text, 0.0)
        s = max(-1.0, min(1.0, s))
        return [s, math.sqrt(max(0.0, 1.0 - s * s))]

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
    p1, p2, p3 = _gate_patches([existing])
    with p1, p2, p3:
        result = await evaluate_gates(candidate)
    assert result.outcome == GateOutcome.LINK_DUPLICATE
    assert result.duplicate_of == 7


@pytest.mark.asyncio
async def test_gate_g1_near_duplicate_that_contradicts_is_a_correction():
    """A changed number embeds like the stale fact. It must supersede, not reinforce."""
    existing = SimpleNamespace(id=7, statement="Embeddings dimension is 2048")
    candidate = ConsolidationCandidate(
        statement="Embeddings dimension is 4096",
        evidence=[{"event_id": 1}, {"event_id": 2}],
        severity="low",
    )
    embedder = _similarity_embedder({"Embeddings dimension is 2048": 0.97, "Embeddings dimension is 4096": 1.0})
    embedder.model = "test-mock"
    p1, p2, p3 = _gate_patches([existing], summarize=await _contradiction([7]), embedder=embedder)
    with p1, p2, p3:
        result = await evaluate_gates(candidate)
    assert result.outcome == GateOutcome.SUPERSEDE
    assert result.supersedes == [7] and result.duplicate_of is None
    assert any("treated as a correction" in r for r in result.reasons)
    assert sum("G3" in r for r in result.reasons) == 1  # the G1 verdict is reused, not re-asked


@pytest.mark.asyncio
async def test_gate_g1_near_duplicate_links_when_contradiction_check_fails():
    """Fail closed to the conservative outcome: link (reinforce), never rewrite on a guess."""
    existing = SimpleNamespace(id=7, statement="Embeddings dimension is 2048")
    candidate = ConsolidationCandidate(
        statement="Embeddings dimension is 4096",
        evidence=[{"event_id": 1}, {"event_id": 2}],
        severity="low",
    )
    embedder = _similarity_embedder({"Embeddings dimension is 2048": 0.97, "Embeddings dimension is 4096": 1.0})
    embedder.model = "test-mock"

    async def broken(text, **kwargs):
        raise RuntimeError("llm down")

    p1, p2, p3 = _gate_patches([existing], summarize=broken, embedder=embedder)
    with p1, p2, p3:
        result = await evaluate_gates(candidate)
    assert result.outcome == GateOutcome.LINK_DUPLICATE and result.duplicate_of == 7


@pytest.mark.asyncio
async def test_gate_g1_detects_paraphrase():
    """G1 must catch near-duplicates, not just identical strings.

    Real paraphrases score 0.85-0.95 with production embeddings (measured
    2026-10-05 on 7 paraphrase pairs). A test using only identical strings
    (similarity exactly 1.0) cannot detect a broken threshold — it passes
    even when the gate is effectively disabled.
    """
    existing = SimpleNamespace(id=7, statement="Postgres runs on port 5433 in docker compose")
    candidate = ConsolidationCandidate(
        statement="In docker-compose, postgres listens on 5433",
        evidence=[{"event_id": 1}, {"event_id": 2}],
        severity="low",
    )
    # Paraphrase similarity 0.90: above the 0.85 threshold, must be caught.
    embedder = _similarity_embedder({
        "Postgres runs on port 5433 in docker compose": 0.90,
        "In docker-compose, postgres listens on 5433": 1.0,
    })
    # The mock needs a model attribute for the stored-embedding fast path.
    embedder.model = "test-mock"
    p1, p2, p3 = _gate_patches([existing], embedder=embedder)
    with p1, p2, p3:
        result = await evaluate_gates(candidate)
    assert result.outcome == GateOutcome.LINK_DUPLICATE
    assert result.duplicate_of == 7


@pytest.mark.asyncio
async def test_gate_g1_ignores_unrelated():
    """G1 must not flag unrelated learnings as duplicates."""
    existing = SimpleNamespace(id=7, statement="The embeddings dimension is 2048")
    candidate = ConsolidationCandidate(
        statement="Backups go to ~/workspace/backups daily",
        evidence=[{"event_id": 1}, {"event_id": 2}],
        severity="low",
    )
    embedder = _similarity_embedder({
        "The embeddings dimension is 2048": 0.08,
        "Backups go to ~/workspace/backups daily": 1.0,
    })
    embedder.model = "test-mock"
    with patch(
        "brain.memory.consolidation.LearningStore.list_active_learnings",
        new=AsyncMock(return_value=[existing]),
    ), patch(
        "brain.memory.consolidation.get_embedding_provider",
        return_value=embedder,
    ):
        result = await evaluate_gates(candidate)
    # G1 passes; G2 has enough evidence, so the candidate promotes.
    assert result.outcome == GateOutcome.PROMOTE
    assert any("G1 pass" in r for r in result.reasons)


@pytest.mark.asyncio
async def test_gate_g1_threshold_is_meaningful():
    """The dedup threshold must actually discriminate.

    A similarity just below the threshold passes; just above is caught.
    This fails if the threshold comparison is broken (e.g. >= 1.0).
    """
    from brain.memory.consolidation import DEDUP_SIMILARITY_THRESHOLD

    for sim, expect_duplicate in [
        (DEDUP_SIMILARITY_THRESHOLD - 0.05, False),
        (DEDUP_SIMILARITY_THRESHOLD + 0.05, True),
    ]:
        existing = SimpleNamespace(id=7, statement="stored learning")
        candidate = ConsolidationCandidate(
            statement="candidate statement",
            evidence=[{"event_id": 1}, {"event_id": 2}],
            severity="low",
        )
        embedder = _similarity_embedder({
            "stored learning": sim,
            "candidate statement": 1.0,
        })
        embedder.model = "test-mock"
        p1, p2, p3 = _gate_patches([existing], embedder=embedder)
        with p1, p2, p3:
            result = await evaluate_gates(candidate)
        if expect_duplicate:
            assert result.outcome == GateOutcome.LINK_DUPLICATE, f"sim={sim}"
        else:
            assert result.outcome != GateOutcome.LINK_DUPLICATE, f"sim={sim}"


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


def _port_candidate(**overrides):
    kwargs = dict(
        statement="Never use port 5432 for postgres",
        evidence=[{"event_id": 1}, {"event_id": 2}],
        severity="low",
    )
    kwargs.update(overrides)
    return ConsolidationCandidate(**kwargs)


async def _contradiction(conflicting_ids=None):
    async def fake_summarize(text, **kwargs):
        data = {"contradicts": True, "reason": "direct conflict on port"}
        if conflicting_ids is not None:
            data["conflicting_ids"] = conflicting_ids
        return json.dumps(data)

    return fake_summarize


@pytest.mark.asyncio
async def test_gate_g3_contradiction_supersedes_named_learning():
    # A contradiction corrects the old learning: validated, low severity → SUPERSEDE.
    existing = SimpleNamespace(id=3, statement="Always use port 5432 for postgres")
    p1, p2, p3 = _gate_patches([existing], summarize=await _contradiction([3]))
    with p1, p2, p3:
        result = await evaluate_gates(_port_candidate())
    assert result.outcome == GateOutcome.SUPERSEDE
    assert result.supersedes == [3]
    assert any("G3: corrects learning(s) [3]" in r for r in result.reasons)


@pytest.mark.asyncio
async def test_gate_g3_ignores_conflicting_ids_outside_active_set():
    # The LLM may hallucinate ids; only active learnings can be superseded.
    existing = SimpleNamespace(id=3, statement="Always use port 5432 for postgres")
    p1, p2, p3 = _gate_patches([existing], summarize=await _contradiction([99, "x"]))
    with p1, p2, p3:
        result = await evaluate_gates(_port_candidate())
    # Orthogonal embedder: best similarity ≈ 0 < SUPERSEDE_MIN_SIMILARITY → unresolved.
    assert result.outcome == GateOutcome.NEEDS_APPROVAL
    assert result.supersedes == []
    assert any("unresolved contradiction" in r for r in result.reasons)


@pytest.mark.asyncio
async def test_gate_g3_falls_back_to_nearest_learning_by_similarity():
    existing = SimpleNamespace(id=3, statement="Always use port 5432 for postgres")
    candidate = _port_candidate()
    embedder = _similarity_embedder({candidate.statement: 1.0, existing.statement: 0.6})
    p1, p2, p3 = _gate_patches([existing], summarize=await _contradiction(), embedder=embedder)
    with p1, p2, p3:
        result = await evaluate_gates(candidate)
    assert result.outcome == GateOutcome.SUPERSEDE
    assert result.supersedes == [3]


@pytest.mark.asyncio
async def test_gate_g3_supersede_on_unvalidated_evidence_needs_approval():
    existing = SimpleNamespace(id=3, statement="Always use port 5432 for postgres")
    candidate = _port_candidate(
        evidence=[{"event_id": 1}, {"event_id": 2}, {"event_id": 3}], evidence_tier=EVIDENCE_TIER_UNVALIDATED,
    )
    p1, p2, p3 = _gate_patches([existing], summarize=await _contradiction([3]))
    with p1, p2, p3:
        result = await evaluate_gates(candidate)
    assert result.outcome == GateOutcome.NEEDS_APPROVAL
    # The correction is recorded so a human approval still retires learning 3.
    assert result.supersedes == [3]
    assert any("unvalidated evidence" in r for r in result.reasons)


@pytest.mark.asyncio
async def test_gate_g3_contradiction_check_fails_open():
    existing = SimpleNamespace(id=3, statement="Always use port 5432 for postgres")

    async def broken(text, **kwargs):
        raise RuntimeError("llm down")

    p1, p2, p3 = _gate_patches([existing], summarize=broken)
    with p1, p2, p3:
        result = await evaluate_gates(_port_candidate())
    assert result.outcome == GateOutcome.PROMOTE
    assert result.supersedes == []


@pytest.mark.asyncio
async def test_gate_g2_unvalidated_evidence_needs_three_events():
    two = ConsolidationCandidate(
        statement="Observed but never proven", severity="low",
        evidence=[{"event_id": 1}, {"event_id": 2}], evidence_tier=EVIDENCE_TIER_UNVALIDATED,
    )
    three = ConsolidationCandidate(
        statement="Observed but never proven", severity="low",
        evidence=[{"event_id": 1}, {"event_id": 2}, {"event_id": 3}], evidence_tier=EVIDENCE_TIER_UNVALIDATED,
    )
    p1, p2, p3 = _gate_patches([])
    with p1, p2, p3:
        rejected = await evaluate_gates(two)
        promoted = await evaluate_gates(three)
    assert rejected.outcome == GateOutcome.REJECT_EVIDENCE
    assert any("need 3" in r for r in rejected.reasons)
    assert promoted.outcome == GateOutcome.PROMOTE


def _principal_candidate(*authors, validated=True):
    evidence = [{"event_id": i + 1, **({"author_principal_id": a} if a is not None else {})}
                for i, a in enumerate(authors)]
    return ConsolidationCandidate(
        statement="Workers need a restart after a settings change", severity="low", evidence=evidence,
        evidence_tier=EVIDENCE_TIER_VALIDATED if validated else EVIDENCE_TIER_UNVALIDATED,
    )


@pytest.mark.asyncio
async def test_gate_g2_team_state_needs_two_distinct_principals():
    """One person's repeated claim is not independent evidence in a team."""
    p1, p2, p3 = _gate_patches([])
    with p1, p2, p3:
        one_voice = await evaluate_gates(_principal_candidate(11, 11), team_state="team")
        anonymous = await evaluate_gates(_principal_candidate(None, None), team_state="team")
        two_voices = await evaluate_gates(_principal_candidate(11, 12), team_state="team")
        three_unvalidated = await evaluate_gates(_principal_candidate(11, 11, 12, validated=False),
                                                 team_state="team")
    assert one_voice.outcome == GateOutcome.REJECT_EVIDENCE
    assert any("only 1 distinct principal(s)" in r and "need 2 in team state" in r for r in one_voice.reasons)
    assert anonymous.outcome == GateOutcome.REJECT_EVIDENCE
    assert any("only 0 distinct principal(s)" in r for r in anonymous.reasons)
    assert two_voices.outcome == GateOutcome.PROMOTE
    assert any("from 2 distinct principal(s)" in r for r in two_voices.reasons)
    assert three_unvalidated.outcome == GateOutcome.PROMOTE


@pytest.mark.asyncio
async def test_gate_g2_solo_state_keeps_the_event_count_rule():
    """A team of one cannot produce two principals; solo state (and no state) only counts events."""
    p1, p2, p3 = _gate_patches([])
    with p1, p2, p3:
        solo = await evaluate_gates(_principal_candidate(11, 11), team_state="solo")
        unknown = await evaluate_gates(_principal_candidate(None, None))
    assert solo.outcome == GateOutcome.PROMOTE
    assert unknown.outcome == GateOutcome.PROMOTE
    assert not any("distinct principal" in r for r in solo.reasons + unknown.reasons)


@pytest.mark.asyncio
async def test_gate_g2_principal_check_is_configurable_and_bypassed_by_approval():
    p1, p2, p3 = _gate_patches([])
    with p1, p2, p3:
        with patch("brain.memory.consolidation.settings.MEMORY_CONSOLIDATION_MIN_DISTINCT_PRINCIPALS_SOLO", 2):
            strict_solo = await evaluate_gates(_principal_candidate(11, 11), team_state="solo")
        with patch("brain.memory.consolidation.settings.MEMORY_CONSOLIDATION_MIN_DISTINCT_PRINCIPALS_TEAM", 0):
            relaxed_team = await evaluate_gates(_principal_candidate(11, 11), team_state="team")
        # A human decides on pending episodes, so the independence check steps aside.
        pending = await evaluate_gates(_principal_candidate(11, 11), require_approval=True, team_state="team")
    assert strict_solo.outcome == GateOutcome.REJECT_EVIDENCE
    assert relaxed_team.outcome == GateOutcome.PROMOTE
    assert pending.outcome == GateOutcome.NEEDS_APPROVAL


@pytest.mark.asyncio
async def test_gate_g4_medium_severity_on_unvalidated_evidence_needs_approval():
    candidate = ConsolidationCandidate(
        statement="Always restart the worker after deploy", severity="medium",
        evidence=[{"event_id": 1}, {"event_id": 2}, {"event_id": 3}], evidence_tier=EVIDENCE_TIER_UNVALIDATED,
    )
    validated = ConsolidationCandidate(
        statement="Always restart the worker after deploy", severity="medium",
        evidence=[{"event_id": 1}, {"event_id": 2}], evidence_tier=EVIDENCE_TIER_VALIDATED,
    )
    p1, p2, p3 = _gate_patches([])
    with p1, p2, p3:
        pending = await evaluate_gates(candidate)
        auto = await evaluate_gates(validated)
    assert pending.outcome == GateOutcome.NEEDS_APPROVAL
    assert any("medium severity on unvalidated" in r for r in pending.reasons)
    assert auto.outcome == GateOutcome.PROMOTE


def test_evidence_tier_for_cluster():
    assert evidence_tier_for([_event(1), _event(2)]) == EVIDENCE_TIER_VALIDATED
    # One unvalidated sighting makes the whole cluster unvalidated.
    assert evidence_tier_for([_event(1), _event(2, validated=False)]) == EVIDENCE_TIER_UNVALIDATED
    legacy = SimpleNamespace(id=9, payload_json={"summary": "old event without the flag"})
    assert evidence_tier_for([legacy]) == EVIDENCE_TIER_UNVALIDATED


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
    assert candidate.evidence[0]["validated"] is True
    assert candidate.evidence_tier == EVIDENCE_TIER_VALIDATED
    assert candidate.suggested_target == "learning"


@pytest.mark.asyncio
async def test_distill_candidate_reads_target_and_evidence_tier():
    cluster = [_event(summary="always run ruff before commit", validated=False),
               _event(event_id=2, summary="ruff first", validated=True)]

    async def fake_summarize(text, **kwargs):
        return json.dumps({"statement": "Run ruff before committing", "category": "workflow",
                           "confidence": 0.8, "severity": "low", "target": "rule"})

    with patch(
        "brain.memory.consolidation.get_summarizer_provider",
        return_value=SimpleNamespace(summarize=fake_summarize),
    ):
        candidate = await distill_candidate(cluster)
    assert candidate.suggested_target == "rule"
    assert candidate.evidence_tier == EVIDENCE_TIER_UNVALIDATED
    # Evidence refs carry no author unless the L1 payload named one.
    assert all("author_principal_id" not in ref for ref in candidate.evidence)

    async def bogus_target(text, **kwargs):
        return json.dumps({"statement": "x", "category": "infra", "target": "database"})

    with patch(
        "brain.memory.consolidation.get_summarizer_provider",
        return_value=SimpleNamespace(summarize=bogus_target),
    ):
        candidate = await distill_candidate(cluster)
    assert candidate.suggested_target == "learning"


@pytest.mark.asyncio
async def test_distill_candidate_copies_authors_into_evidence():
    cluster = [_event(event_id=1, author=11), _event(event_id=2, author=12), _event(event_id=3)]

    async def fake_summarize(text, **kwargs):
        return json.dumps({"statement": "dim is 2048", "category": "infra", "confidence": 0.9})

    with patch(
        "brain.memory.consolidation.get_summarizer_provider",
        return_value=SimpleNamespace(summarize=fake_summarize),
    ):
        candidate = await distill_candidate(cluster)
    by_event = {ref["event_id"]: ref.get("author_principal_id") for ref in candidate.evidence}
    assert by_event == {1: 11, 2: 12, 3: None}


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


def _fake_lock_engine(acquired=True):
    """Stand-in for brain.database.session.async_engine: the advisory lock always succeeds."""
    conn = AsyncMock()
    conn.execute.return_value = SimpleNamespace(scalar=lambda: acquired)
    engine = SimpleNamespace(connect=lambda: _AsyncCtx(conn))
    return engine, conn


class _AsyncCtx:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_):
        return False


@pytest.mark.asyncio
async def test_run_consolidation_empty_episodes():
    distill = AsyncMock(return_value={"status": "ok", "groups": 0, "created": [], "skipped": []})
    engine, lock_conn = _fake_lock_engine()
    with patch(
        "brain.database.session.async_engine", engine
    ), patch(
        "brain.memory.consolidation.collect_l1_events", new=AsyncMock(return_value=([], {}))
    ), patch(
        "brain.memory.consolidation.count_pending_episodes", new=AsyncMock(return_value=4)
    ), patch(
        "brain.memory.skill_distiller.distill_skills", new=distill
    ), patch(
        "brain.memory.consolidation.settings.MEMORY_SKILL_DISTILLATION_ENABLED", True
    ):
        report = await run_consolidation()
    assert report["l1_events"] == 0
    assert report["episodes_created"] == 0
    assert report["candidates"] == 0
    assert report["promoted"] == []
    assert report["reinforced"] == []
    # The run tail always reports the human queue and runs L3→L4 distillation.
    assert report["pending_total"] == 4
    assert report["skills"]["status"] == "ok"
    distill.assert_awaited_once()
    # Lock taken and released.
    assert lock_conn.execute.await_count == 2


@pytest.mark.asyncio
async def test_run_consolidation_skips_when_lock_is_held():
    engine, _ = _fake_lock_engine(acquired=False)
    collect = AsyncMock(return_value=([], {}))
    with patch("brain.database.session.async_engine", engine), patch(
        "brain.memory.consolidation.collect_l1_events", new=collect
    ):
        report = await run_consolidation()
    assert report["status"] == "skipped" and report["reinforced"] == []
    collect.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_tail_is_best_effort():
    engine, _ = _fake_lock_engine()
    with patch(
        "brain.database.session.async_engine", engine
    ), patch(
        "brain.memory.consolidation.collect_l1_events", new=AsyncMock(return_value=([], {}))
    ), patch(
        "brain.memory.consolidation.count_pending_episodes", new=AsyncMock(side_effect=RuntimeError("db"))
    ), patch(
        "brain.memory.skill_distiller.distill_skills", new=AsyncMock(side_effect=RuntimeError("llm"))
    ), patch(
        "brain.memory.consolidation.settings.MEMORY_SKILL_DISTILLATION_ENABLED", True
    ):
        report = await run_consolidation()
    assert "pending_total" not in report
    assert report["skills"]["status"] == "failed"
    assert report["status"] == "ok"

    with patch(
        "brain.database.session.async_engine", engine
    ), patch(
        "brain.memory.consolidation.collect_l1_events", new=AsyncMock(return_value=([], {}))
    ), patch(
        "brain.memory.consolidation.count_pending_episodes", new=AsyncMock(return_value=0)
    ), patch(
        "brain.memory.consolidation.settings.MEMORY_SKILL_DISTILLATION_ENABLED", True
    ):
        dry = await run_consolidation(dry_run=True)
    assert "skills" not in dry
