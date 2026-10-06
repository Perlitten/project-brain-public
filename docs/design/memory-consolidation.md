# Memory Consolidation Layer — Design Proposal

**Status:** implemented (taxonomy updated 2026-10-06) · **Date:** 2026-10-05 · **Author:** Muse (for Andrey)

> **Memory tiers (canonical, used everywhere in code and docs):**
>
> | Tier | Name | Storage | Written by |
> |---|---|---|---|
> | L1 | working | `agent_task_events` (`classification in ('learning','failure_lesson')`) | task runtime |
> | L2 | episodic | `memory_episodes` + `memory_episode_events` (L1 consumption ledger) | consolidation job |
> | L3 | semantic | `memory_learnings` (`LearningStore`) | consolidation (auto/approved) or `POST /learnings` |
> | L4 | procedural | `memory_skills` | `POST /skills` |
>
> Consolidation is the *process* between L1 and L3, not a tier. Earlier drafts
> below called L1 "episodic" and L2 "consolidation"; read those as L1 working /
> L2 episodic. Each L1 event is consumed by at most one L2 episode (primary key
> on `memory_episode_events.event_id`), so re-runs are idempotent. Episode
> status: `pending` (G4, awaiting `POST /episodes/{id}/approve|reject`),
> `promoted`, `rejected`, `duplicate` (G1, `duplicate_of_learning_id` set).

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
│  L1 · WORKING (raw)  │  agent_task_events, MemoryHandoff   (exists, extend)
└─────────┬───────────┘
          │  capture learnings with classification='learning'
          ▼
┌─────────────────────┐
│ L2 · EPISODIC       │  memory_episodes, via brain/memory/consolidation.py
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

### L1 — Working (extend, don't rebuild)

- `agent_task_events.classification` already exists (`observation` default).
  Add a convention: agents log `classification='learning'` for durable-worthy
  observations (with `payload_json.evidence`), and `'failure_lesson'` for
  post-mortems of failed validations/tasks.
- `MemoryHandoff.summary` already captures session digests — the
  consolidation job reads these as a second episodic source.
- No schema change required for the MVP; a classification convention +
  documentation is enough.

### L2 — Episodic, written by consolidation (new)

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

## 6. Implemented semantics (repo scoping + L4 rollout)

- **`repo_scope` population**: L1 events carry no repo column; a cluster's
  scope is the modal normalized `agent_tasks.repo_path` of its L1 events
  (`task_id` join; orphan events → NULL). The episode, and any learning it
  promotes to (including the approve-endpoint path), inherit it. Skills take
  `repo_scope` from the request, normalized at write time.
- **Backfill**: `_backfill_memory_repo_scope` derives existing episodes from
  their `source_event_ids` → `agent_task_events` → `agent_tasks.repo_path`
  (modal repo), and skills from `source_episode_ids`. Rows with no derivable
  scope (deleted tasks, hand-registered skills) stay NULL — NULL means
  *global*, visible to every repo. That is the documented semantic, not a gap.
- **Re-open rule**: a `rejected` episode keeps its L1 events consumed unless
  new evidence arrives — when a fresh L1 event lands in the episode's repo
  scope within `MEMORY_EPISODE_REOPEN_DAYS` (default 14, 0 disables) of the
  rejection, its ledger rows are released and the events re-cluster. A
  NULL-scope episode re-opens on any fresh event.
- **L4 in /ask and the runtime context** (`MEMORY_SKILLS_IN_ASK=false`
  default): `brain/memory/skill_store.py` holds matching; `/ask` injects the
  top `MEMORY_SKILLS_IN_ASK_TOP` active skills as a `### Procedures` block
  capped at `MEMORY_SKILLS_IN_ASK_MAX_BYTES` and returns `skills_used`;
  `RuntimeContextBuilder` adds a `procedures` section inside the same byte
  budget. Both filter strictly by repo scope: global (NULL) plus the request
  repo only — a skill scoped elsewhere is never injected. With the flag off,
  /ask context and response shape are unchanged (no Procedures section,
  `skills_used` is an empty list).
