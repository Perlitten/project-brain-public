"""L2 memory consolidation: distill episodic traces into durable learnings.

Pipeline: collect (L1 episodic) → cluster → distill (LLM) → gate (G1–G4) → promote (L3).

Promotion gates are lexicographic, mirroring brain/improvement/promotion.py:
a candidate must pass every gate in order; the first failure decides the outcome.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from brain.database.harness_models import AgentTaskEvent
from brain.database.session import async_session_factory
from brain.llm import get_embedding_provider, get_summarizer_provider
from brain.memory.learning_store import LearningStore

logger = logging.getLogger(__name__)

# Episodic classifications that feed consolidation (L1 → L2).
LEARNING_CLASSIFICATIONS = ("learning", "failure_lesson")

# G1: cosine similarity at or above this means "already known".
DEDUP_SIMILARITY_THRESHOLD = 0.92

# G2: minimum independent episodes for auto-promotion (1 episode needs human confirm).
MIN_EPISODES_AUTO = 2


class GateOutcome(str, Enum):
    PROMOTE = "promote"
    LINK_DUPLICATE = "link_duplicate"      # G1: already known
    REJECT_EVIDENCE = "reject_evidence"    # G2: not enough evidence
    REJECT_CONTRADICTION = "reject_contradiction"  # G3: conflicts with active memory
    NEEDS_APPROVAL = "needs_approval"      # G4: high-stakes, human must confirm


@dataclass
class ConsolidationCandidate:
    statement: str
    category: Optional[str] = None
    confidence: float = 0.5
    evidence: List[Dict[str, Any]] = field(default_factory=list)
    severity: str = "low"  # low | medium | high
    suggested_target: str = "learning"  # learning | decision | rule


@dataclass
class GateResult:
    outcome: GateOutcome
    reasons: List[str] = field(default_factory=list)
    duplicate_of: Optional[int] = None


def _cosine(a: List[float], b: List[float]) -> float:
    denom = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(x * x for x in b))
    if denom == 0:
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / denom


def _episode_text(event: AgentTaskEvent) -> str:
    payload = event.payload_json or {}
    parts = [event.event_type, event.classification]
    for key in ("summary", "statement", "lesson", "text", "detail"):
        if payload.get(key):
            parts.append(str(payload[key]))
    return " | ".join(p for p in parts if p)


async def collect_episodic(since: Optional[datetime] = None) -> List[AgentTaskEvent]:
    """Collect unprocessed L1 episodic records eligible for consolidation."""
    async with async_session_factory() as session:
        query = select(AgentTaskEvent).where(
            AgentTaskEvent.classification.in_(LEARNING_CLASSIFICATIONS)
        )
        if since:
            query = query.where(AgentTaskEvent.created_at >= since)
        query = query.order_by(AgentTaskEvent.created_at)
        result = await session.execute(query)
        return list(result.scalars().all())


async def cluster_episodes(
    episodes: List[AgentTaskEvent],
) -> List[List[AgentTaskEvent]]:
    """Greedy clustering of episodes by embedding similarity."""
    if not episodes:
        return []
    embedder = get_embedding_provider()
    texts = [_episode_text(e) for e in episodes]
    vectors = await embedder.embed_batch(texts)
    clusters: List[List[int]] = []
    centroids: List[List[float]] = []
    for idx, vec in enumerate(vectors):
        placed = False
        for c_idx, centroid in enumerate(centroids):
            if _cosine(vec, centroid) >= 0.82:
                clusters[c_idx].append(idx)
                members = clusters[c_idx]
                centroids[c_idx] = [
                    sum(vectors[m][d] for m in members) / len(members)
                    for d in range(len(vec))
                ]
                placed = True
                break
        if not placed:
            clusters.append([idx])
            centroids.append(vec)
    return [[episodes[i] for i in cluster] for cluster in clusters]


_DISTILL_PROMPT = """Distill the following episodic observations from an AI coding harness
into ONE durable learning: a single factual statement that will stay true and
useful for future sessions.

Episodes:
{episodes}

Reply with JSON only: {{"statement": "...", "category": "...", "confidence": 0.0-1.0, "severity": "low|medium|high"}}
Category is one of: infra, llm-behavior, repo, workflow, testing.
Severity is "high" only if acting on a wrong statement would cause damage."""


async def distill_candidate(cluster: List[AgentTaskEvent]) -> ConsolidationCandidate:
    """LLM-distill one cluster of episodes into a candidate learning."""
    episodes_text = "\n".join(f"- {_episode_text(e)}" for e in cluster)
    summarizer = get_summarizer_provider()
    raw = await summarizer.summarize(_DISTILL_PROMPT.format(episodes=episodes_text))
    try:
        data = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        data = {}
    evidence = [
        {"event_id": e.id, "task_id": str(e.task_id), "classification": e.classification}
        for e in cluster
    ]
    return ConsolidationCandidate(
        statement=str(data.get("statement", episodes_text[:500])).strip(),
        category=data.get("category"),
        confidence=float(data.get("confidence", 0.5) or 0.5),
        evidence=evidence,
        severity=data.get("severity", "low") if data.get("severity") in ("low", "medium", "high") else "low",
    )


_CONTRADICTION_PROMPT = """You check whether a new learning contradicts existing durable memory.

New learning: {statement}

Existing memory:
{existing}

Reply with JSON only: {{"contradicts": true|false, "reason": "..."}}"""


async def evaluate_gates(
    candidate: ConsolidationCandidate,
    *,
    require_approval: bool = False,
) -> GateResult:
    """Run lexicographic promotion gates G1–G4. First failure decides."""
    reasons: List[str] = []

    # G1 — Dedup against active learnings.
    active = await LearningStore.list_active_learnings()
    if active:
        embedder = get_embedding_provider()
        cand_vec = await embedder.embed(candidate.statement)
        act_vecs = await embedder.embed_batch([lrng.statement for lrng in active])
        best_idx, best_sim = -1, -1.0
        for i, vec in enumerate(act_vecs):
            sim = _cosine(cand_vec, vec)
            if sim > best_sim:
                best_idx, best_sim = i, sim
        if best_sim >= DEDUP_SIMILARITY_THRESHOLD:
            return GateResult(
                outcome=GateOutcome.LINK_DUPLICATE,
                reasons=[f"G1: similarity {best_sim:.2f} to learning {active[best_idx].id}"],
                duplicate_of=active[best_idx].id,
            )
    reasons.append("G1 pass: no duplicate")

    # G2 — Evidence threshold.
    episode_count = len(candidate.evidence)
    if episode_count < MIN_EPISODES_AUTO and not require_approval:
        return GateResult(
            outcome=GateOutcome.REJECT_EVIDENCE,
            reasons=reasons + [f"G2: only {episode_count} episode(s), need {MIN_EPISODES_AUTO}"],
        )
    reasons.append(f"G2 pass: {episode_count} episode(s)")

    # G3 — Non-contradiction with active memory.
    if active:
        summarizer = get_summarizer_provider()
        existing_text = "\n".join(f"- [{lrng.id}] {lrng.statement}" for lrng in active[:20])
        try:
            raw = await summarizer.summarize(
                _CONTRADICTION_PROMPT.format(statement=candidate.statement, existing=existing_text)
            )
            data = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
            if data.get("contradicts"):
                return GateResult(
                    outcome=GateOutcome.REJECT_CONTRADICTION,
                    reasons=reasons + [f"G3: contradicts active memory: {data.get('reason', '')}"],
                )
        except Exception as exc:
            logger.warning("G3 contradiction check failed, failing open: %s", exc)
    reasons.append("G3 pass: no contradiction")

    # G4 — Human approval for high severity.
    if candidate.severity == "high" or require_approval:
        return GateResult(
            outcome=GateOutcome.NEEDS_APPROVAL,
            reasons=reasons + ["G4: high severity requires human approval"],
        )
    reasons.append("G4 pass: auto-promotable")

    return GateResult(outcome=GateOutcome.PROMOTE, reasons=reasons)


async def promote_candidate(
    candidate: ConsolidationCandidate,
    gate: GateResult,
    run_id: Optional[str] = None,
) -> Optional[int]:
    """Write a gated candidate to L3. Returns the learning id, or None."""
    if gate.outcome != GateOutcome.PROMOTE:
        return None
    return await LearningStore.add_learning(
        statement=candidate.statement,
        category=candidate.category,
        confidence=candidate.confidence,
        evidence=candidate.evidence,
        promoted_from=run_id,
    )


async def run_consolidation(
    since: Optional[datetime] = None,
    *,
    require_approval: bool = False,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Run one consolidation pass. Returns a summary report."""
    run_id = f"consolidation-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    episodes = await collect_episodic(since)
    report: Dict[str, Any] = {
        "run_id": run_id,
        "episodes": len(episodes),
        "candidates": 0,
        "promoted": [],
        "rejected": [],
        "needs_approval": [],
    }
    if not episodes:
        return report
    clusters = await cluster_episodes(episodes)
    for cluster in clusters:
        candidate = await distill_candidate(cluster)
        report["candidates"] += 1
        gate = await evaluate_gates(candidate, require_approval=require_approval)
        entry: Dict[str, Any] = {"statement": candidate.statement, "reasons": gate.reasons}
        if gate.outcome == GateOutcome.PROMOTE:
            if dry_run:
                entry["dry_run"] = True
                report["promoted"].append(entry)
            else:
                learning_id = await promote_candidate(candidate, gate, run_id)
                entry["learning_id"] = learning_id
                report["promoted"].append(entry)
        elif gate.outcome == GateOutcome.NEEDS_APPROVAL:
            report["needs_approval"].append(entry)
        else:
            report["rejected"].append(entry)
    logger.info(
        "Consolidation %s: %d episodes → %d candidates, %d promoted",
        run_id, len(episodes), report["candidates"], len(report["promoted"]),
    )
    return report
