# Memory Consolidation Layer — Design Proposal

**Status:** proposal · **Date:** 2026-10-05 · **Author:** Muse (for Andrey)

## 1. The gap

Project Brain has three kinds of "memory" today, but no pipeline between the
raw and the durable:

| Layer | What | Where | Written by |
|---|---|---|---|
| Code index | what the code *is* | Postgres + pgvector, Neo4j | indexer (rebuilt from git) |
| Normative memory | what we *decided*, what *rules* hold | `DecisionStore`, `RuleStore` | humans/agents, explicitly |
| Episodic traces | what *happened* | `agent_task_events` (+`classification`), `MemoryHandoff` | task runtime |

What is missing: **nothing distills episodic traces into durable knowledge.**
An agent that learns "the NVIDIA embeddings dimension is 2048, not 1536"
during a task has nowhere to put it except a human-written decision or rule.
The next session rediscovers it from scratch.

This proposal adds the missing middle: a consolidation pipeline
**episodic → candidate → promotion gates → semantic**, reusing patterns the
codebase already has.

## 2. Proposed architecture

```
┌─────────────────────┐
│  L1 · EPISODIC (raw) │  agent_task_events, MemoryHandoff   (exists, extend)
└─────────┬───────────┘
          │  capture learnings with classification='learning'
          ▼
┌─────────────────────┐
│ L2 · CONSOLIDATION  │  NEW: brain/memory/consolidation.py
│  (periodic job)     │  distill → dedup → gate → promote
└─────────┬───────────┘
          │  promotion gates (lexicographic, cf. improvement/promotion.py)
          ▼
┌─────────────────────┐
│ L3 · SEMANTIC       │  DecisionStore, RuleStore (exist)
│  (durable)          │  NEW: LearningStore — general durable facts
└─────────────────────┘
          │
          ▼  retrieval: context_pack_builder + relevance.py (extend)
```

### L1 — Episodic (extend, don't rebuild)

- `agent_task_events.classification` already exists (`observation` default).
  Add a convention: agents log `classification='learning'` for durable-worthy
  observations (with `payload_json.evidence`), and `'failure_lesson'` for
  post-mortems of failed validations/tasks.
- `MemoryHandoff.summary` already captures session digests — the
  consolidation job reads these as a second episodic source.
- No schema change required for the MVP; a classification convention +
  documentation is enough.

### L2 — Consolidation (new)

New module `brain/memory/consolidation.py`, driven by the existing worker
`scheduler` pattern (`brain/workers/tasks.py`, `params["scheduled"]`).
Runs daily (configurable), each run recorded in `memory_consolidation_runs`.

Pipeline per run:

1. **Collect** — unprocessed L1 records since last run: events with
   `classification IN ('learning','failure_lesson')`, recent handoffs,
   failed `AgentValidationResult`s.
2. **Cluster** — group by embedding similarity (reuse the embeddings
   preset); each cluster = one candidate learning. Near-duplicates of an
   *active* L3 learning are linked, not duplicated.
3. **Distill** — one LLM call per cluster (summarizer preset) →
   candidate `{statement, category, confidence, evidence_refs,
   suggested_target}` where `suggested_target ∈ {learning, decision, rule}`.
4. **Gate** — lexicographic promotion gates (mirroring
   `LexicographicPromotionEngine`):
   - G1 Dedup: max similarity to active learnings < threshold, else link.
   - G2 Evidence: ≥2 independent episodes, or 1 episode + human confirm.
   - G3 Non-contradiction: no conflict with active decisions/rules
     (LLM contradiction check, cheap model).
   - G4 Human approval for `severity ≥ high`; low-stakes auto-promote.
   - Rejected candidates are kept with reason (audit, cf. `brain/audit/`).
5. **Promote** — write to `LearningStore` (or route to `DecisionStore` /
   `RuleStore` when the candidate is really a decision/rule).

### L3 — Semantic (one new store)

```sql
CREATE TABLE memory_learnings (
    id            BIGSERIAL PRIMARY KEY,
    statement     TEXT NOT NULL,          -- the durable fact
    category      VARCHAR(64),            -- e.g. 'infra', 'llm-behavior', 'repo'
    confidence    FLOAT NOT NULL DEFAULT 0.5,
    evidence      JSONB NOT NULL,         -- [{task_id, event_id, ...}]
    status        VARCHAR(16) NOT NULL DEFAULT 'active',
                      -- active | superseded | rejected
    superseded_by BIGINT REFERENCES memory_learnings(id),
    valid_until   TIMESTAMPTZ,            -- optional decay
    repo_scope    VARCHAR(1024),          -- reuse normalize_repo_scope
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    promoted_from VARCHAR(64)            -- consolidation run id
);
CREATE INDEX ON memory_learnings USING ivfflat (embedding vector_cosine_ops);
```

`LearningStore` mirrors the `DecisionStore`/`RuleStore` shape: thin
classmethod wrapper around `async_session_factory`, repo-scoped reads.

Supersede, don't delete: when a new learning contradicts an old one, the old
row becomes `superseded` with a link — full audit trail.

### Retrieval

`context_pack_builder._load_active_normative_memory` already injects
rules + decisions into every context pack. Extend it:

- load active learnings scoped to the repo (`repo_scope` clause, same as
  decisions),
- rank with `brain/memory/relevance.py` + embedding similarity to the query,
- cap by token budget (`brain/context/budget.py`).

Stale learnings decay: `valid_until` or supersede chain excludes them
automatically.

## 3. Work plan

1. **Migration + `LearningStore`** — new table, store class mirroring
   `RuleStore` (~150 lines). No behavior change.
2. **L1 convention** — document `classification='learning'` /
   `'failure_lesson'`; add to agent prompt / harness docs. Seed 2–3
   learnings manually to prove retrieval (e.g. "embeddings dim is 2048").
3. **Consolidation job** — `brain/memory/consolidation.py` + worker task
   wiring + `memory_consolidation_runs` table. Gates G1–G4.
4. **Retrieval wiring** — extend `_load_active_normative_memory`, budget
   accounting.
5. **Eval** — add a benchmark task type `memory`: plant a learning in L1,
   run consolidation, assert `/ask` recalls it a week later (ground truth
   via `evidence` links).

## 4. Explicit non-goals

- No automatic rewriting of decisions/rules — candidates *route* to a human
  or to the existing stores' approval flow.
- No cross-repo leakage: `repo_scope` applies exactly like decisions.
- No silent forgetting: supersede chain is append-only.

## 5. Why this fits the codebase

- Promotion gates reuse the lexicographic pattern from
  `brain/improvement/promotion.py` (already reviewed, already tested).
- Stores reuse the `async_session_factory` classmethod-wrapper shape.
- Scheduling reuses the worker `scheduled` task pattern.
- Retrieval reuses `relevance.py`, `repo_scope`, and the context-pack
  budget system.
- The benchmark methodology from `eval/harness_benchmark/` extends
  naturally to a `memory` task type with verifiable ground truth.
