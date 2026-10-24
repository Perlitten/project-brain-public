"""Agent/endpoint budgets and lab sandbox fields."""
from __future__ import annotations

from pydantic import field_validator


class SettingsAgent:
    AGENT_LOCATOR_MAX_BYTES: int = 2_400

    AGENT_RUNTIME_CONTEXT_MAX_BYTES: int = 26_000

    AGENT_ASK_INPUT_MAX_BYTES: int = 18_000

    AGENT_ASK_OUTPUT_MAX_BYTES: int = 2_000

    AGENT_IMPACT_MAX_BYTES: int = 8_000

    AGENT_RETRIEVAL_DEADLINE_S: float = 15.0

    AGENT_CONTEXT_DEADLINE_S: float = 30.0

    AGENT_ASK_DEADLINE_S: float = 25.0

    AGENT_IMPACT_DEADLINE_S: float = 10.0

    # Docker is the sole containment backend. Missing/invalid backends fail
    # closed; unsafe host execution requires both mode=off and explicit opt-in.
    LAB_ALLOW_UNSAFE_HOST_EXECUTION: bool = False

    LAB_SANDBOX_MODE: str = "auto"

    LAB_SANDBOX_IMAGE: str = "python:3.12-slim"

    LAB_SANDBOX_PIDS_LIMIT: int = 256

    LAB_SANDBOX_MEMORY_MB: int = 2048

    LAB_SANDBOX_CPUS: float = 2.0

    # Extra host paths (JSON list or CSV of absolute dirs) mounted read-only /
    # re-exposed inside the sandbox — deployment-specific toolchains.
    LAB_SANDBOX_EXTRA_MOUNTS: list[str] = []

    @field_validator(
        "AGENT_LOCATOR_MAX_BYTES",
        "AGENT_RUNTIME_CONTEXT_MAX_BYTES",
        "AGENT_ASK_INPUT_MAX_BYTES",
        "AGENT_ASK_OUTPUT_MAX_BYTES",
        "AGENT_IMPACT_MAX_BYTES",
    )
    @classmethod
    def validate_agent_budget(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("agent byte budgets must be positive")
        return value
