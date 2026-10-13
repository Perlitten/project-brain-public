# Memory Semantics

L1 reports, L2 episodes, L3 guidance and L4 procedures have different admission
and approval rules. Evidence-linked lessons record **reported validation**;
they do not prove that Brain ran a test. Eligible L3 clusters can promote
automatically by default, including low-severity unvalidated clusters that meet
the stricter evidence threshold. High-risk cases require approval; distilled L4
procedures default to pending approval. Direct rule/decision writes and curated
procedures are separate paths. The complete policy, including contradiction
failure behavior, is in [MEMORY_LESSON_PROTOCOL.md](MEMORY_LESSON_PROTOCOL.md).

Explicitly historical Markdown remains searchable but is tagged as historical
evidence and heavily demoted in ordinary current-state retrieval. A query that
explicitly asks for history or archived context restores its normal retrieval
weight. Decision memory is canonical by normalized title within an optional
`repo_path`: recording the same title in the same scope updates that decision
instead of creating another active instruction. Context packs include global
decisions plus decisions owned by the requested repository, so one project's
normative memory cannot leak into another project's task.
Rules and decisions are read fresh for each context pack; only repository files
and symbols use the process cache, so a write or revocation takes effect on the
next delegated task without an API restart.
Time-bound audit snapshots, archived design sources, and repository Task
Contracts receive the same non-current authority treatment automatically; the
live harness injects its exact active contract separately.

**Brain Insights** is a proactive inbox backed by the worker queue and the `insights` database table, shown in the web UI. Manual scans enqueue `proactive_insights`; recurring jobs (nightly maintenance, health, self-diagnosis, weekly benchmark) are fired by the scheduler inside the worker — see Settings → Scheduler and [ADR 013](adr/013-worker-scheduler.md).

---

