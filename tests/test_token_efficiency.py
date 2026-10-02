"""Unit and integration tests for Token-Efficient External Memory System."""
from brain.context.budget import estimated_tokens, utf8_bytes
from brain.context.context_cache import clear_context_cache, get_cached_context, put_cached_context
from brain.llm.observability import audit_and_bound_llm_input, HARD_MAX_INPUT_TOKENS, TARGET_INPUT_TOKENS
from brain.operations.incidents import IncidentStateMachine, IncidentSeverity
from brain.operations.remediation import AutonomicRemediationEngine


def test_observability_audit_and_bounding():
    """Verify that audit_and_bound_llm_input measures tokens and caps input to 16,000 tokens."""
    prompt = "Simple user question about codebase"
    sys_inst = "You are a concise software assistant."

    bounded_p, bounded_sys, audit = audit_and_bound_llm_input(
        prompt,
        sys_inst,
        target_tokens=TARGET_INPUT_TOKENS,
        max_tokens=HARD_MAX_INPUT_TOKENS,
        lineage_verified=True,
    )

    assert bounded_p == prompt
    assert bounded_sys == sys_inst
    assert audit.total_tokens <= TARGET_INPUT_TOKENS
    assert audit.trimmed is False
    assert audit.lineage_verified is True


def test_observability_hard_cap_trimming():
    """Verify that oversized inputs exceeding 16,000 tokens are deterministically trimmed."""
    large_prompt = "Query: Explain this large code block\n" + ("def fn(): return 'repeat'\n" * 3000)
    sys_inst = "System instruction"

    raw_tokens = estimated_tokens(utf8_bytes(large_prompt + sys_inst))
    assert raw_tokens > HARD_MAX_INPUT_TOKENS

    bounded_p, bounded_sys, audit = audit_and_bound_llm_input(
        large_prompt,
        sys_inst,
        target_tokens=TARGET_INPUT_TOKENS,
        max_tokens=HARD_MAX_INPUT_TOKENS,
    )

    assert audit.total_tokens <= HARD_MAX_INPUT_TOKENS
    assert audit.trimmed is True
    assert len(audit.dropped_items) > 0
    assert audit.dropped_items[0]["reason"] == "hard_token_cap_16k_exceeded"


def test_context_caching_and_repeat_deduplication():
    """Verify that context caching returns stored payload on equivalent repeated queries."""
    clear_context_cache()

    repo = "project-brain"
    query = "How to configure FastAPI router middleware?"
    rev = "6e9aba9f1624"

    # Miss before put
    assert get_cached_context(repo, query, rev) is None

    payload = {"status": "ok", "slices": [{"path": "apps/api/main.py", "range": [1, 50]}]}
    put_cached_context(repo, query, payload, rev)

    # Hit after put
    cached = get_cached_context(repo, query, rev)
    assert cached is not None
    assert cached["status"] == "ok"

    # Invalidation on revision mismatch
    assert get_cached_context(repo, query, "different_sha") is None


def test_retrieval_flood_incident_deduplication():
    """Verify that retrieval flood incidents are deduplicated to at most 1 actionable notification."""
    sm = IncidentStateMachine()
    remediation = AutonomicRemediationEngine()

    inc1, notify1 = sm.report_root_incident(
        incident_type="RETRIEVAL_FLOOD_CONTAINED",
        repository_id="project-brain",
        root_cause_summary="Forced retrieval flood self-repaired via trimming",
        severity=IncidentSeverity.WARNING,
    )
    assert notify1 is True

    inc2, notify2 = sm.report_root_incident(
        incident_type="RETRIEVAL_FLOOD_CONTAINED",
        repository_id="project-brain",
        root_cause_summary="Duplicate retrieval flood notification",
        severity=IncidentSeverity.WARNING,
    )
    assert notify2 is False

    # Execute remediation recipe
    res = remediation.execute_remediation(inc1, lineage_verified=True)
    assert res.success is True
    assert res.recipe_id == "REC-RETRIEVAL-FLOOD-CONTAINMENT"
