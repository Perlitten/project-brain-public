"""Real Provider Token Telemetry & Cost Accounting for Autonomous Operator."""
from typing import Dict, Any
from brain.autonomy.models import TelemetryRecord, GoalSession


def record_llm_telemetry(
    session: GoalSession,
    call_id: str,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
    latency_ms: float = 0.0,
) -> TelemetryRecord:
    billable_in = max(0, input_tokens - cached_tokens)
    cost = (billable_in * 2.50 / 1_000_000) + (cached_tokens * 1.25 / 1_000_000) + (output_tokens * 10.00 / 1_000_000)
    rec = TelemetryRecord(
        call_id=call_id,
        provider=provider,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        total_tokens=input_tokens + output_tokens,
        api_cost_usd=round(cost, 6),
        latency_ms=round(latency_ms, 2),
    )
    session.telemetry.append(rec)
    return rec


def summarize_session_telemetry(session: GoalSession) -> Dict[str, Any]:
    total_input = sum(t.input_tokens for t in session.telemetry)
    total_output = sum(t.output_tokens for t in session.telemetry)
    total_cached = sum(t.cached_tokens for t in session.telemetry)
    total_cost = sum(t.api_cost_usd for t in session.telemetry)
    total_calls = len(session.telemetry)
    return {
        "call_count": total_calls,
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_cached_tokens": total_cached,
        "total_tokens": total_input + total_output,
        "total_api_cost_usd": round(total_cost, 6),
    }
