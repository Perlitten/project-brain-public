"""Memory consolidation: turn L1 working events into L2 episodes and L3 learnings.

Memory tiers: L1 working (``agent_task_events``), L2 episodic (``memory_episodes``),
L3 semantic (``memory_learnings``), L4 procedural (``memory_skills``).

Pipeline: collect unconsumed L1 events → cluster → distill (LLM) → gate (G1–G4)
→ persist one L2 episode per cluster (+ L3 learning when promoted), atomically.

Promotion gates are lexicographic, mirroring brain/improvement/promotion.py:
a candidate must pass every gate in order; the first failure decides the outcome.
"""

from __future__ import annotations

import json
import logging
import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import delete, select

from brain.config.settings import settings
from brain.database.harness_models import AgentTask, AgentTaskEvent
from brain.database.models import MemoryEpisode, MemoryEpisodeEvent
from brain.database.session import async_session_factory
from brain.llm import get_embedding_provider, get_summarizer_provider
from brain.memory.learning_store import LearningStore
from brain.memory.repo_scope import normalize_repo_scope

logger = logging.getLogger(__name__)

# L1 event classifications that feed consolidation (L1 → L2).
LEARNING_CLASSIFICATIONS = ("learning", "failure_lesson")

# G1: cosine similarity at or above this means "already known".
# Calibrated 2026-10-05 on 7 paraphrase pairs + 6 unrelated pairs:
# 0.85 gives recall 0.86 with zero false positives (F1 0.92);
# 0.92 dropped recall to 0.71. Unrelated pairs max out at ~0.12.
DEDUP_SIMILARITY_THRESHOLD = 0.85

# Postgres advisory lock id serializing consolidation runs across processes.
CONSOLIDATION_LOCK_ID = 0x4D454D01  # "MEM\x01"

# G2: minimum independent L1 events for auto-promotion (1 event needs human confirm).
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


def _l1_event_text(event: AgentTaskEvent) -> str:
    payload = event.payload_json or {}
    parts = [event.event_type, event.classification]
    for key in ("summary", "statement", "lesson", "text", "detail"):
        if payload.get(key):
            parts.append(str(payload[key]))
    return " | ".join(p for p in parts if p)


async def _collect_unconsumed_l1_events(
    session,
    since: Optional[datetime],
) -> Tuple[List[AgentTaskEvent], Dict[int, Optional[str]]]:
    """Unconsumed L1 events plus each event's normalized repository scope.

    L1 events carry no repo column; their scope is derived through
    ``task_id`` → ``agent_tasks.repo_path``. An outer join keeps events whose
    task row is gone — they consolidate as unscoped (NULL repo_scope)."""
    consumed = select(MemoryEpisodeEvent.event_id)
    query = (
        select(AgentTaskEvent, AgentTask.repo_path)
        .outerjoin(AgentTask, AgentTaskEvent.task_id == AgentTask.id)
        .where(
            AgentTaskEvent.classification.in_(LEARNING_CLASSIFICATIONS),
            AgentTaskEvent.id.not_in(consumed),
        )
    )
    if since:
        query = query.where(AgentTaskEvent.created_at >= since)
    query = query.order_by(AgentTaskEvent.created_at, AgentTaskEvent.id)
    result = await session.execute(query)
    events: List[AgentTaskEvent] = []
    repo_by_event: Dict[int, Optional[str]] = {}
    for event, repo_path in result.all():
        events.append(event)
        repo_by_event[event.id] = normalize_repo_scope(repo_path)
    return events, repo_by_event


async def _reopen_rejected_episode_events(
    session,
    fresh_repos: set,
) -> int:
    """Release the L1 events of recently rejected episodes back for re-clustering.

    A rejected episode's events stay consumed forever unless new evidence shows
    up: when a fresh L1 event arrives in the episode's repository scope within
    ``MEMORY_EPISODE_REOPEN_DAYS`` of the rejection, the episode's ledger rows
    are deleted and its events join the next clustering pass. Scopes match
    exactly: a NULL-scope episode re-opens only on a fresh NULL-scope event,
    never on an event belonging to a repository. 0 days disables re-opening.
    """
    days = settings.MEMORY_EPISODE_REOPEN_DAYS
    if days <= 0 or not fresh_repos:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rejected = (
        await session.execute(
            select(MemoryEpisode.id, MemoryEpisode.repo_scope).where(
                MemoryEpisode.status == "rejected",
                MemoryEpisode.updated_at >= cutoff,
            )
        )
    ).all()
    reopen_ids = [
        episode_id
        for episode_id, scope in rejected
        if normalize_repo_scope(scope) in fresh_repos
    ]
    if not reopen_ids:
        return 0
    deleted = await session.execute(
        delete(MemoryEpisodeEvent).where(MemoryEpisodeEvent.episode_id.in_(reopen_ids))
    )
    await session.commit()
    logger.info(
        "Re-opened %d rejected episodes (%d L1 events) for re-clustering",
        len(reopen_ids),
        deleted.rowcount or 0,
    )
    return deleted.rowcount or 0


async def collect_l1_events(
    since: Optional[datetime] = None,
) -> Tuple[List[AgentTaskEvent], Dict[int, Optional[str]]]:
    """Collect L1 events eligible for consolidation, with per-event repo scope.

    Returns the unconsumed events and a ``event_id → normalized repo_path`` map.
    Rejected-episode re-opening (``MEMORY_EPISODE_REOPEN_DAYS``) runs here so the
    released events are included in this pass's count and clustering."""
    async with async_session_factory() as session:
        events, repo_by_event = await _collect_unconsumed_l1_events(session, since)
        # Keep NULL in the set: a NULL-scope event may re-open a NULL-scope
        # episode, but never a scoped one (NULL matches only NULL).
        fresh_repos = set(repo_by_event.values())
        reopened = await _reopen_rejected_episode_events(session, fresh_repos)
        if reopened:
            events, repo_by_event = await _collect_unconsumed_l1_events(session, since)
        return events, repo_by_event


async def cluster_l1_events(
    events: List[AgentTaskEvent],
) -> List[List[AgentTaskEvent]]:
    """Greedy clustering of L1 events by embedding similarity."""
    if not events:
        return []
    embedder = get_embedding_provider()
    texts = [_l1_event_text(e) for e in events]
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
    return [[events[i] for i in cluster] for cluster in clusters]


_DISTILL_PROMPT = """Distill the following episodic observations from an AI coding harness
into ONE durable learning: a single factual statement that will stay true and
useful for future sessions.

Episodes:
{episodes}

Reply with JSON only: {{"statement": "...", "category": "...", "confidence": 0.0-1.0, "severity": "low|medium|high"}}
Category is one of: infra, llm-behavior, repo, workflow, testing.
Severity is "high" only if acting on a wrong statement would cause damage."""


async def distill_candidate(cluster: List[AgentTaskEvent]) -> ConsolidationCandidate:
    """LLM-distill one cluster of L1 events into a candidate learning."""
    events_text = "\n".join(f"- {_l1_event_text(e)}" for e in cluster)
    summarizer = get_summarizer_provider()
    raw = await summarizer.summarize(_DISTILL_PROMPT.format(episodes=events_text))
    try:
        data = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        data = {}
    evidence = [
        {"event_id": e.id, "task_id": str(e.task_id), "classification": e.classification}
        for e in cluster
    ]
    return ConsolidationCandidate(
        statement=str(data.get("statement", events_text[:500])).strip(),
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
    repo_scope: Optional[str] = None,
) -> GateResult:
    """Run lexicographic promotion gates G1–G4. First failure decides."""
    reasons: List[str] = []

    # G1 — Dedup against active learnings visible to this cluster's repo
    # (global plus same-repo); a learning scoped to another repo must not
    # dedup a candidate it never applied to.
    active = await LearningStore.list_active_learnings(repo_scope)
    if active:
        embedder = get_embedding_provider()
        current_model = getattr(embedder, "model", None) or getattr(
            embedder, "provider", "unknown"
        )
        cand_vec = await embedder.embed(candidate.statement)
        act_vecs: List[List[float]] = []
        missing_idx: List[int] = []
        for i, lrng in enumerate(active):
            vec = getattr(lrng, "embedding", None)
            if vec and getattr(lrng, "embedding_model", None) == current_model and len(vec) == len(cand_vec):
                act_vecs.append(list(vec))
            else:
                missing_idx.append(i)
                act_vecs.append([])
        if missing_idx:
            batch = await embedder.embed_batch([active[i].statement for i in missing_idx])
            for i, vec in zip(missing_idx, batch):
                act_vecs[i] = vec
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
    event_count = len(candidate.evidence)
    if event_count < MIN_EPISODES_AUTO and not require_approval:
        return GateResult(
            outcome=GateOutcome.REJECT_EVIDENCE,
            reasons=reasons + [f"G2: only {event_count} L1 event(s), need {MIN_EPISODES_AUTO}"],
        )
    reasons.append(f"G2 pass: {event_count} L1 event(s)")

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


async def _derive_candidate_repo_scope(candidate: ConsolidationCandidate) -> Optional[str]:
    """Modal repository scope of a candidate's L1 evidence events.

    Same derivation as episode clustering: ``event_id`` → ``agent_task_events.task_id``
    → ``agent_tasks.repo_path``. None when no evidence event has a known repo
    (the learning then stays unscoped, the honest answer).
    """
    event_ids = []
    for item in candidate.evidence or []:
        try:
            event_ids.append(int(item["event_id"]))
        except (KeyError, TypeError, ValueError):
            continue
    if not event_ids:
        return None
    async with async_session_factory() as session:
        rows = (
            await session.execute(
                select(AgentTaskEvent.id, AgentTask.repo_path)
                .join(AgentTask, AgentTaskEvent.task_id == AgentTask.id)
                .where(AgentTaskEvent.id.in_(event_ids))
            )
        ).all()
    counts = Counter(
        scope for _event_id, repo_path in rows if (scope := normalize_repo_scope(repo_path)) is not None
    )
    if not counts:
        return None
    # Deterministic tie-break: highest count, then lexicographic scope.
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


async def promote_candidate(
    candidate: ConsolidationCandidate,
    gate: GateResult,
    run_id: Optional[str] = None,
    repo_scope: Optional[str] = None,
) -> Optional[int]:
    """Write a gated candidate to L3. Returns the learning id, or None.

    The learning is scoped like an approved episode's: an explicit
    ``repo_scope`` wins (normalized); otherwise it is derived from the
    candidate's evidence events. Underivable scope stays NULL.
    """
    if gate.outcome != GateOutcome.PROMOTE:
        return None
    scope = normalize_repo_scope(repo_scope) if repo_scope else None
    if scope is None:
        try:
            scope = await _derive_candidate_repo_scope(candidate)
        except Exception as exc:
            logger.warning(f"Could not derive repo_scope for promoted candidate: {exc}")
            scope = None
    return await LearningStore.add_learning(
        statement=candidate.statement,
        category=candidate.category,
        confidence=candidate.confidence,
        evidence=candidate.evidence,
        repo_scope=scope,
        promoted_from=run_id,
    )


async def run_consolidation(
    since: Optional[datetime] = None,
    *,
    require_approval: bool = False,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Run one consolidation pass. Returns a summary report.

    Serialized across processes via a Postgres advisory lock: two concurrent
    runs would both pass G1 (check-then-act) and double-promote the same
    candidates. If the lock is held, the run is skipped (not queued).
    """
    from sqlalchemy import text

    from brain.database.session import async_engine

    async with async_engine.connect() as lock_conn:
        try:
            res = await lock_conn.execute(
                text(f"SELECT pg_try_advisory_lock({CONSOLIDATION_LOCK_ID})")
            )
            acquired = res.scalar()
        except Exception as exc:
            logger.warning(f"Consolidation lock check failed: {exc}; running unlocked.")
            acquired = True
        if not acquired:
            logger.info("Consolidation already running elsewhere; skipping this pass.")
            return {
                "run_id": None,
                "status": "skipped",
                "reason": "advisory lock held by concurrent consolidation",
                "l1_events": 0,
                "candidates": 0,
                "episodes_created": 0,
                "errors": [],
                "promoted": [],
                "rejected": [],
                "needs_approval": [],
            }
        try:
            return await _run_consolidation_unlocked(
                since, require_approval=require_approval, dry_run=dry_run
            )
        finally:
            try:
                await lock_conn.execute(
                    text(f"SELECT pg_advisory_unlock({CONSOLIDATION_LOCK_ID})")
                )
                await lock_conn.commit()
            except Exception as exc:
                logger.warning(f"Consolidation lock release failed: {exc}")


_EPISODE_STATUS = {
    GateOutcome.PROMOTE: "promoted",
    GateOutcome.NEEDS_APPROVAL: "pending",
    GateOutcome.LINK_DUPLICATE: "duplicate",
    GateOutcome.REJECT_EVIDENCE: "rejected",
    GateOutcome.REJECT_CONTRADICTION: "rejected",
}


class EpisodeStateError(ValueError):
    """The episode is not in a state that allows the requested transition."""


async def _episode_embedding(statement: str) -> Optional[List[float]]:
    from brain.embeddings.constants import EMBEDDING_DIMENSION
    from brain.embeddings.pgvector_sql import pgvector_index_dimension, truncate_vector_for_index

    try:
        vec = await get_embedding_provider().embed(statement)
    except Exception as exc:
        logger.warning("L2 episode embedding failed, storing NULL: %s", exc)
        return None
    if not vec:
        return None
    if len(vec) < pgvector_index_dimension(EMBEDDING_DIMENSION):
        logger.warning("L2 episode embedding has %d dims, storing NULL", len(vec))
        return None
    return truncate_vector_for_index(list(vec), EMBEDDING_DIMENSION)


def _cluster_repo_scope(
    cluster: List[AgentTaskEvent],
    repo_by_event: Dict[int, Optional[str]],
) -> Optional[str]:
    """Modal repository scope of a cluster; None when no event has a repo."""
    counts = Counter(
        repo for event in cluster if (repo := repo_by_event.get(event.id)) is not None
    )
    if not counts:
        return None
    return counts.most_common(1)[0][0]


async def _persist_cluster(
    cluster: List[AgentTaskEvent],
    candidate: ConsolidationCandidate,
    gate: GateResult,
    run_id: str,
    repo_scope: Optional[str] = None,
) -> Dict[str, Any]:
    """Write one L2 episode, its L1 ledger rows and (if promoted) its L3 learning in one transaction."""
    embedding = await _episode_embedding(candidate.statement)
    async with async_session_factory() as session:
        async with session.begin():
            episode = MemoryEpisode(
                source_event_ids=[e.id for e in cluster],
                distilled_summary=candidate.statement,
                topic=candidate.category,
                status=_EPISODE_STATUS[gate.outcome],
                confidence=candidate.confidence,
                gate_reasons=list(gate.reasons),
                duplicate_of_learning_id=gate.duplicate_of,
                embedding=embedding,
                repo_scope=normalize_repo_scope(repo_scope),
            )
            session.add(episode)
            await session.flush()
            session.add_all(MemoryEpisodeEvent(event_id=e.id, episode_id=episode.id) for e in cluster)
            learning_id = None
            if gate.outcome == GateOutcome.PROMOTE:
                learning_id = await LearningStore.add_learning(
                    statement=candidate.statement,
                    category=candidate.category,
                    confidence=candidate.confidence,
                    evidence=candidate.evidence,
                    repo_scope=episode.repo_scope,
                    promoted_from=run_id,
                    session=session,
                )
                episode.promoted_to_learning_id = learning_id
            await session.flush()
            return {"episode_id": episode.id, "learning_id": learning_id}


async def _run_consolidation_unlocked(
    since: Optional[datetime] = None,
    *,
    require_approval: bool = False,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Run one consolidation pass. Returns a summary report."""
    run_id = f"consolidation-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    events, repo_by_event = await collect_l1_events(since)
    report: Dict[str, Any] = {
        "run_id": run_id,
        "l1_events": len(events),
        "candidates": 0,
        "episodes_created": 0,
        "promoted": [],
        "rejected": [],
        "needs_approval": [],
        "errors": [],
        "dry_run": dry_run,
    }
    if not events:
        return report
    for cluster in await cluster_l1_events(events):
        candidate = await distill_candidate(cluster)
        report["candidates"] += 1
        gate = await evaluate_gates(
            candidate,
            require_approval=require_approval,
            repo_scope=_cluster_repo_scope(cluster, repo_by_event),
        )
        entry: Dict[str, Any] = {"statement": candidate.statement, "reasons": gate.reasons}
        if gate.duplicate_of is not None:
            entry["duplicate_of"] = gate.duplicate_of
        if dry_run:
            entry["dry_run"] = True
        else:
            try:
                entry.update(
                    await _persist_cluster(
                        cluster, candidate, gate, run_id,
                        repo_scope=_cluster_repo_scope(cluster, repo_by_event),
                    )
                )
            except Exception as exc:
                # The transaction rolled back as a whole: no episode, no ledger rows, no
                # learning. The L1 events stay unconsumed and are retried on the next run.
                logger.error("L2 episode write failed for events %s: %s", [e.id for e in cluster], exc)
                report["errors"].append(
                    {"event_ids": [e.id for e in cluster], "error": f"{type(exc).__name__}: {exc}"}
                )
                continue
            report["episodes_created"] += 1
        if gate.outcome == GateOutcome.PROMOTE:
            report["promoted"].append(entry)
        elif gate.outcome == GateOutcome.NEEDS_APPROVAL:
            report["needs_approval"].append(entry)
        else:
            report["rejected"].append(entry)
    logger.info(
        "Consolidation %s: %d L1 events → %d candidates, %d episodes, %d promoted, %d errors",
        run_id, len(events), report["candidates"], report["episodes_created"],
        len(report["promoted"]), len(report["errors"]),
    )
    return report


async def _decide_pending_episode(episode_id: int, *, approve: bool, reason: Optional[str]) -> Dict[str, Any]:
    async with async_session_factory() as session:
        async with session.begin():
            episode = (
                await session.execute(
                    select(MemoryEpisode).where(MemoryEpisode.id == episode_id).with_for_update()
                )
            ).scalar_one_or_none()
            if episode is None:
                raise LookupError(f"Episode {episode_id} not found")
            if episode.status != "pending":
                raise EpisodeStateError(f"Episode {episode_id} is {episode.status}, not pending")
            note = f"{'approved' if approve else 'rejected'} by human" + (f": {reason}" if reason else "")
            episode.gate_reasons = [*(episode.gate_reasons or []), note]
            learning_id = None
            if approve:
                learning_id = await LearningStore.add_learning(
                    statement=episode.distilled_summary,
                    category=episode.topic,
                    confidence=episode.confidence,
                    evidence=[{"event_id": event_id} for event_id in episode.source_event_ids or []],
                    repo_scope=episode.repo_scope,
                    promoted_from=f"approval:episode-{episode.id}",
                    session=session,
                )
                episode.status = "promoted"
                episode.promoted_to_learning_id = learning_id
            else:
                episode.status = "rejected"
            return {"episode_id": episode.id, "status": episode.status, "learning_id": learning_id}


async def approve_episode(episode_id: int, reason: Optional[str] = None) -> Dict[str, Any]:
    """Promote a pending (G4 needs_approval) L2 episode to an L3 learning."""
    return await _decide_pending_episode(episode_id, approve=True, reason=reason)


async def reject_episode(episode_id: int, reason: Optional[str] = None) -> Dict[str, Any]:
    """Reject a pending L2 episode; its L1 events stay consumed."""
    return await _decide_pending_episode(episode_id, approve=False, reason=reason)
