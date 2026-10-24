"""Memory subsystem fields."""
from __future__ import annotations



class SettingsMemory:
    # Memory consolidation loop (L1 working events → L2 episodes → L3 learnings → L4 skills).
    # Manual run via worker job "memory_consolidation" stays available; this
    # gate is for scheduled automation (hourly, brain/workers/scheduler.py).
    # On by default since 2026-10-10: the gates (G1 dedup, G2 evidence tiers,
    # G3 supersede, G4 severity) decide per candidate; REQUIRE_APPROVAL=true
    # forces every candidate through the human queue regardless.
    MEMORY_CONSOLIDATION_ENABLED: bool = True

    MEMORY_CONSOLIDATION_REQUIRE_APPROVAL: bool = False

    # G2 independence: how many distinct principals must stand behind a cluster's
    # evidence before it auto-promotes. Team state defaults to two (one person's
    # repeated claim is not independent evidence) and never goes below two: a
    # cluster short of it becomes a reviewed proposal (docs/MEMORY_REVIEW.md).
    # Solo state, a team of one, keeps the event-count rule alone (0 disables
    # the principal check).
    MEMORY_CONSOLIDATION_MIN_DISTINCT_PRINCIPALS_TEAM: int = 2

    MEMORY_CONSOLIDATION_MIN_DISTINCT_PRINCIPALS_SOLO: int = 0

    # Project promoted learnings the distiller classified as a rule/decision
    # into RuleStore/DecisionStore (best-effort, after the L3 write committed).
    MEMORY_ROUTE_TARGETS: bool = True

    # L3 → L4: distill repeated same-scope workflow/testing/repo learnings into
    # procedural skills at the end of each consolidation run. New skills land
    # as "pending" until approved via POST /skills/{id}/approve.
    MEMORY_SKILL_DISTILLATION_ENABLED: bool = True

    MEMORY_SKILL_DISTILLATION_REQUIRE_APPROVAL: bool = True

    MEMORY_SKILL_DISTILLATION_MIN_LEARNINGS: int = 3

    # Runtime L3 projection (prepare_task_context / compact /ask): add a
    # pgvector pass over stored embeddings before the lexical pass. One query
    # embedding call bounded by the timeout; never backfills missing vectors.
    MEMORY_RETRIEVAL_SEMANTIC: bool = True

    MEMORY_RETRIEVAL_EMBED_TIMEOUT_S: float = 0.8

    # Re-open rule: a rejected L2 episode's L1 events may re-cluster when a new
    # event in the same repository scope arrives within this many days of the
    # rejection. 0 disables re-opening (rejected episodes keep their events).
    MEMORY_EPISODE_REOPEN_DAYS: int = 14

    # L4 procedural memory in /ask and the runtime context: inject the top
    # matching active skills as a procedures block, strictly filtered by repo
    # scope (same repo or explicitly is_global; NULL scope never injected).
    MEMORY_SKILLS_IN_ASK: bool = True

    MEMORY_SKILLS_IN_ASK_TOP: int = 2

    MEMORY_SKILLS_IN_ASK_MAX_BYTES: int = 1500

    # Opt-in: tell the owner chat when a memory proposal awaits review (no draft content is sent).
    MEMORY_REVIEW_TELEGRAM_NOTIFY: bool = False
