# Validated lesson and L4 outcome protocol

`POST /harness/tasks/{task_id}/lessons` (`harness:write`) accepts a statement,
category, validation_id, artifact_ids, source_refs and idempotency_key. Validation
and artifacts must belong to the task. The validation must link to an artifact
in the submitted list; source_refs must equal registered artifact URIs. Scope
comes from the task. These are reported checks, not proof of server execution.
Pass creates a learning event; fail/error creates a failure_lesson. Identical
retries are idempotent, changed content with the same key returns 409.

`POST /harness/tasks/{task_id}/observations` (`harness:write`) is the
unvalidated counterpart: statement, category, optional outcome (`learning` or
`failure_lesson`) and an idempotency_key of at least 8 characters. No
validation or artifacts are required and none are claimed: the L1 event is
written with `reported_validation=false` and `evidence_tier=unvalidated`, scope
from the task; an unknown task is 404. Handoff summaries
(`POST /harness/tasks/{task_id}/handoffs`) are mirrored into the same tier
automatically — one `handoff_summary` L1 event per session and summary,
idempotent, blank summaries skipped — so a session's closing notes reach memory
without a separate call.

## Evidence tiers

Consolidation treats the two tiers differently. A cluster is *validated* only
when every L1 event in it carries `reported_validation=true`; a single
unvalidated event makes the whole cluster unvalidated. Validated clusters may
promote from 2 independent events, unvalidated ones need 3. Events written
before the flag existed carry no flag and are unvalidated. The tier is recorded
on every learning's evidence items (`validated: true|false`).

## Consolidation

Ordinary old events are never reclassified or backfilled as lessons. With
MEMORY_CONSOLIDATION_ENABLED=true (default) an independent hourly job at minute
15 UTC processes eligible L1 events under a Postgres advisory lock; it does not
depend on successful deep/LFM maintenance. A manual run is
POST /jobs/memory-consolidation. The run report lists `promoted`, `reinforced`,
`rejected`, `needs_approval`, `errors`, `pending_total` and the `skills`
distillation tail.

Gates run in order and the first failure decides:

- **G1 dedup** (cosine ≥ 0.85 against active learnings visible to the
  cluster's repo). A confirmed duplicate is *linked*, not dropped: the known
  learning is reinforced (confidence +0.05, evidence appended, statement never
  rewritten). Because a corrected number or flag embeds almost identically to
  the stale statement, a near-duplicate is first checked for contradiction; a
  contradicting one is treated as a correction and continues to G2–G4 instead
  of reinforcing the wrong fact. If that check fails, the conservative outcome
  is to link.
- **G2 evidence**: 2 validated or 3 unvalidated events (see tiers). Bypassed
  when the operator forces approval, because a human decides then. In team
  state the events must also come from at least 2 distinct principals
  (`MEMORY_CONSOLIDATION_MIN_DISTINCT_PRINCIPALS_TEAM`; the solo setting defaults
  to 0 = off): one person's repeated claim is not independent evidence.
- **G3 contradiction**: the summarizer names the active learnings the new
  statement contradicts; unnamed contradictions fall back to the nearest
  learning when it is at least 0.5 similar. A resolved contradiction
  *supersedes* the old learning (append-only `superseded_by` chain, applied in
  the same transaction as the episode and new learning, so a crash leaves
  either both or neither). An unresolved one goes to approval. A failed check
  fails open.
- **G4 approval**. MEMORY_CONSOLIDATION_REQUIRE_APPROVAL=false (default) lets
  eligible low-severity clusters promote automatically once their validated or
  unvalidated evidence threshold is met; set it to true to require approval for
  new promotions through POST /episodes/{id}/approve. Duplicate reinforcement
  follows G1 instead of creating a new promotion. Regardless of the
  flag these stay pending: high severity; medium severity backed only by
  unvalidated evidence; a supersede backed only by unvalidated evidence; an
  unresolved contradiction. Approving a pending correction applies its
  recorded supersede; rejecting leaves active memory untouched.

A distilled statement the model classifies as a `rule` or `decision` is also
projected into the Rule or Decision store after the transaction commits
(MEMORY_ROUTE_TARGETS, default true, best-effort; the learning stays
canonical and the report shows `routed_to`).

## Retrieval

Compact context reads active, unexpired, scoped L3 guidance live, alongside
applicable rules and decisions. Retrieval is hybrid: one bounded
query-embedding call (MEMORY_RETRIEVAL_EMBED_TIMEOUT_S, default 0.8 s) scores
learnings that carry a stored embedding of the current model via pgvector, and
a lexical Unicode pass scores statements; ranking is 0.6 cosine + 0.3 lexical +
0.1 confidence, a learning seen only by the semantic pass must reach cosine
0.45, with up to 64 candidates, 3 selected records and 1200 bytes. There is no
backfill: without an embedder, on timeout, or with
MEMORY_RETRIEVAL_SEMANTIC=false the function degrades to the lexical pass
alone. Cached code evidence never caches memory status. Learned guidance is
historical advice; current code claims still require current slices.

## L4 procedures

L4 procedures come from two paths. Explicit curation through POST /skills is
unchanged. At the end of every consolidation run (also callable on its own)
L3 → L4 distillation (MEMORY_SKILL_DISTILLATION_ENABLED, default true) looks
for at least MEMORY_SKILL_DISTILLATION_MIN_LEARNINGS (default 3, never below 2)
active same-repo learnings in procedural categories (workflow, testing, repo)
that no skill has consumed yet, and asks the summarizer for one named
procedure per repo, at most 5 per run. Facts the model marks non-procedural are
skipped; a name that already exists folds its learnings into the existing
skill instead of retrying forever. Distilled skills are `pending`
(MEMORY_SKILL_DISTILLATION_REQUIRE_APPROVAL, default true) and are never
matched or injected until `POST /skills/{skill_id}/approve`;
`POST /skills/{skill_id}/reject` retires them. MEMORY_SKILLS_IN_ASK=true
(default) injects bounded applicable same-repo or explicitly global active
procedures. Unscoped non-global procedures never qualify.

L4 outcomes use `POST /skills/{skill_id}/outcomes` (`core:write`). The caller must
provide a task, a task-owned validation result, non-empty `artifact_ids`, and
`source_refs`. `success` requires validation status `pass`; `failure` accepts
`fail`, `error`, or `pass`. The endpoint records `reported_validation=true`;
it does not claim that the server executed the procedure. A task can have one
outcome per skill. Repeating the same evidence is idempotent; changing the
evidence or result returns conflict. Retrieval never changes usage counters.
Counters describe reported outcomes, not independently adjudicated efficacy.

Every memory object (episode, learning, skill, decision, rule) carries
`author_principal_id` / `updated_by_principal_id` and `created_at` / `updated_at`;
L1 events carry the author in their payload. Decisions and rules never overwrite
in place: a changed re-post keeps the old content in a `superseded` history row,
bumps `version` and names the editor, and an optional `expected_version`
(compare-and-swap, 0 = must not exist) returns 409 on a mismatch. In team state
the author of the evidence may not approve their own pending episode or skill.
`POST /decisions` and `POST /rules` record the authenticated principal as the
author (`author_principal_id`; NULL for the legacy shared key and CLI writes).
A `system_global` decision additionally needs the `core:admin` scope, which the
legacy shared key and `*` credentials hold; a plain `core:write` credential
receives 403. The body carries no role: `caller_role` in a request is ignored.

## MCP

Local and remote MCP expose record_task_lesson, record_task_observation and
record_procedure_outcome. Restart an existing MCP client to refresh its
catalogue. Lesson and observation capture need harness:write in addition to
the normal core:write,jobs:read scopes; do not expand unrelated credentials.

Bounded /ask supports an explicit LLM_TASK_ASK_MODEL without changing global
synthesis. LLM_TASK_ASK_ENABLE_THINKING=false is sent only to supported NVIDIA
Nemotron-3 models as chat_template_kwargs.enable_thinking. Unset preserves the
provider default. Timeout, length completion, unfinished/empty reasoning and
answer truncation return partial, with actual finish_reason and token usage
when available. This alone does not prove end-to-end coding token savings.
