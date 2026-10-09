# Validated lesson and L4 outcome protocol

`POST /harness/tasks/{task_id}/lessons` (`harness:write`) accepts a statement,
category, validation_id, artifact_ids, source_refs and idempotency_key. Validation
and artifacts must belong to the task. The validation must link to an artifact
in the submitted list; source_refs must equal registered artifact URIs. Scope
comes from the task. These are reported checks, not proof of server execution.
Pass creates a learning event; fail/error creates a failure_lesson. Identical
retries are idempotent, changed content with the same key returns 409.

Ordinary old events are never reclassified or backfilled as lessons. With
MEMORY_CONSOLIDATION_ENABLED=true, an independent hourly job at minute 15 UTC
processes eligible L1 events. It does not depend on successful deep/LFM
maintenance. Keep MEMORY_CONSOLIDATION_REQUIRE_APPROVAL=true: eligible L2
episodes remain pending until a human approves them through
POST /episodes/{id}/approve. Promotion gates still apply. A manual run is
POST /jobs/memory-consolidation.

Compact context reads active, unexpired, scoped L3 guidance live, alongside
applicable rules and decisions. The hot path uses lexical Unicode relevance,
up to 64 candidates, 3 selected records and 1200 bytes, without embedding calls
or backfills. Synonym-only discovery remains on the legacy semantic surface.
Cached code evidence never caches memory status. Learned guidance is historical
advice; current code claims still require current slices.

L4 procedures are explicitly curated through POST /skills, rather than
automatically distilled from every observation. MEMORY_SKILLS_IN_ASK=true
injects bounded applicable same-repo or explicitly global procedures.
Unscoped non-global procedures never qualify.

L4 outcomes use `POST /skills/{skill_id}/outcomes` (`core:write`). The caller must
provide a task, a task-owned validation result, non-empty `artifact_ids`, and
`source_refs`. `success` requires validation status `pass`; `failure` accepts
`fail`, `error`, or `pass`. The endpoint records `reported_validation=true`;
it does not claim that the server executed the procedure. A task can have one
outcome per skill. Repeating the same evidence is idempotent; changing the
evidence or result returns conflict. Retrieval never changes usage counters.
Counters describe reported outcomes, not independently adjudicated efficacy.

Local and remote MCP expose record_task_lesson and record_procedure_outcome.
Restart an existing MCP client to refresh its catalogue. Lesson capture needs
harness:write in addition to the normal core:write,jobs:read scopes; do not
expand unrelated credentials.

Bounded /ask supports an explicit LLM_TASK_ASK_MODEL without changing global
synthesis. LLM_TASK_ASK_ENABLE_THINKING=false is sent only to supported NVIDIA
Nemotron-3 models as chat_template_kwargs.enable_thinking. Unset preserves the
provider default. Timeout, length completion, unfinished/empty reasoning and
answer truncation return partial, with actual finish_reason and token usage
when available. This alone does not prove end-to-end coding token savings.
