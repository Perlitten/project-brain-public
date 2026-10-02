"""Unit and integration tests for Autonomous Self-Healing Engineering Operator."""
import pytest
from brain.autonomy.models import GoalSession, GoalPhase, EscalationReason
from brain.autonomy.operator_engine import AutonomousOperatorEngine
from brain.autonomy.state import SessionStore
from brain.autonomy.telemetry import record_llm_telemetry, summarize_session_telemetry


def test_session_store_crud(tmp_path):
    """Verify durable SessionStore save/load cycle for crash-resumable execution."""
    store = SessionStore(storage_dir=tmp_path)
    session = GoalSession(session_id="test_sess_01", goal_description="Fix short SHA comparator bug")

    store.save(session)
    loaded = store.load("test_sess_01")

    assert loaded is not None
    assert loaded.session_id == "test_sess_01"
    assert loaded.goal_description == "Fix short SHA comparator bug"
    assert loaded.current_phase == GoalPhase.GOAL_RECEIVED


@pytest.mark.asyncio
async def test_operator_engine_closed_loop(tmp_path):
    """Verify autonomous operator executes closed loop to CLOSED phase."""
    store = SessionStore(storage_dir=tmp_path)
    engine = AutonomousOperatorEngine(session_store=store)

    session = engine.start_session("goal_01", "Implement Champion-Anchored League Tournament Engine")
    assert session.current_phase == GoalPhase.GOAL_RECEIVED

    resolved_session = await engine.execute_goal(
        "goal_01",
        oracle_test="tests/improvement/test_holdout_sealing.py::test_multiple_challengers_can_never_access_sealed_pool_in_league",
    )

    assert resolved_session.is_closed is True
    assert resolved_session.current_phase == GoalPhase.CLOSED
    assert len(resolved_session.checkpoints) >= 5
    assert len(resolved_session.telemetry) > 0


def test_operator_escalation_boundary(tmp_path):
    """Verify strict escalation boundary logs justification when safety boundary is breached."""
    store = SessionStore(storage_dir=tmp_path)
    engine = AutonomousOperatorEngine(session_store=store)

    engine.start_session("esc_goal", "Destructive production database drop requested")
    escalated = engine.escalate_boundary(
        "esc_goal",
        reason=EscalationReason.DESTRUCTIVE_PRODUCTION_ACTION,
        description="Dropping production PostgreSQL database is a hard safety boundary",
        provenance_proof="Action payload contains DROP DATABASE production",
    )

    assert escalated.is_blocked is True
    assert escalated.current_phase == GoalPhase.BLOCKED_ESCALATED
    assert len(escalated.escalations) == 1
    assert escalated.escalations[0].reason == EscalationReason.DESTRUCTIVE_PRODUCTION_ACTION


def test_real_provider_telemetry_accounting():
    """Verify real provider token telemetry records billable input tokens, output tokens, and cost."""
    session = GoalSession(session_id="telemetry_test", goal_description="Telemetry check")
    rec = record_llm_telemetry(
        session=session,
        call_id="call_01",
        provider="openai",
        model="gpt-4o",
        input_tokens=4000,
        output_tokens=500,
        cached_tokens=1000,
        latency_ms=1200.0,
    )

    assert rec.input_tokens == 4000
    assert rec.output_tokens == 500
    assert rec.cached_tokens == 1000
    assert rec.total_tokens == 4500
    assert rec.api_cost_usd > 0.0

    summary = summarize_session_telemetry(session)
    assert summary["call_count"] == 1
    assert summary["total_input_tokens"] == 4000
    assert summary["total_output_tokens"] == 500
