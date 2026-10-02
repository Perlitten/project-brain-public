"""LLM pre-call token observability, section budgeting, and deterministic bounds enforcement."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional
from loguru import logger

from brain.context.budget import BudgetExceeded, truncate_utf8, utf8_bytes, estimated_tokens

TARGET_INPUT_TOKENS = 8000
HARD_MAX_INPUT_TOKENS = 16000

# 1 token approx 4 bytes UTF-8
TARGET_INPUT_BYTES = TARGET_INPUT_TOKENS * 4
HARD_MAX_INPUT_BYTES = HARD_MAX_INPUT_TOKENS * 4


@dataclass
class LLMCallAudit:
    """Pre-call diagnostic audit for every LLM generation attempt."""

    total_tokens: int
    prompt_tokens: int
    system_tokens: int
    total_bytes: int
    target_tokens: int = TARGET_INPUT_TOKENS
    max_tokens: int = HARD_MAX_INPUT_TOKENS
    lineage_verified: bool = True
    cache_hit: bool = False
    sections: dict[str, int] = field(default_factory=dict)
    dropped_items: list[dict[str, str]] = field(default_factory=list)
    trimmed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_tokens": self.total_tokens,
            "prompt_tokens": self.prompt_tokens,
            "system_tokens": self.system_tokens,
            "total_bytes": self.total_bytes,
            "target_tokens": self.target_tokens,
            "max_tokens": self.max_tokens,
            "lineage_verified": self.lineage_verified,
            "cache_hit": self.cache_hit,
            "sections": self.sections,
            "dropped_items": self.dropped_items,
            "trimmed": self.trimmed,
        }


def audit_and_bound_llm_input(
    prompt: str,
    system_instruction: Optional[str] = None,
    *,
    target_tokens: int = TARGET_INPUT_TOKENS,
    max_tokens: int = HARD_MAX_INPUT_TOKENS,
    lineage_verified: bool = True,
    cache_hit: bool = False,
) -> tuple[str, Optional[str], LLMCallAudit]:
    """Audit, log, and deterministically bound LLM prompt payload before API call.

    Guarantees that no model call exceeds ``max_tokens`` (default 16,000).
    Applies deterministic evidence trimming if input exceeds the cap.
    """
    sys_bytes = utf8_bytes(system_instruction or "")
    prompt_bytes = utf8_bytes(prompt)
    total_bytes = sys_bytes + prompt_bytes
    total_tokens = estimated_tokens(total_bytes)

    sys_tokens = estimated_tokens(sys_bytes)
    p_tokens = estimated_tokens(prompt_bytes)

    sections: dict[str, int] = {
        "system_instruction": sys_tokens,
        "prompt": p_tokens,
    }
    dropped_items: list[dict[str, str]] = []
    trimmed = False

    bounded_prompt = prompt
    bounded_sys = system_instruction

    max_bytes = max_tokens * 4

    if total_bytes > max_bytes:
        trimmed = True
        excess_bytes = total_bytes - max_bytes
        # Retain system instruction; trim prompt evidence tail
        allowed_prompt_bytes = max(100, prompt_bytes - excess_bytes - 64)
        bounded_prompt = truncate_utf8(prompt, allowed_prompt_bytes, suffix="\n[... Context trimmed to fit hard 16k token limit ...]")
        dropped_items.append({
            "section": "prompt_evidence_tail",
            "reason": "hard_token_cap_16k_exceeded",
            "bytes_removed": str(prompt_bytes - utf8_bytes(bounded_prompt)),
        })
        prompt_bytes = utf8_bytes(bounded_prompt)
        total_bytes = sys_bytes + prompt_bytes
        total_tokens = estimated_tokens(total_bytes)
        p_tokens = estimated_tokens(prompt_bytes)
        sections["prompt"] = p_tokens

    if total_tokens > max_tokens:
        raise BudgetExceeded(
            f"LLM input token count ({total_tokens}) exceeds hard cap of {max_tokens} tokens"
        )

    audit = LLMCallAudit(
        total_tokens=total_tokens,
        prompt_tokens=p_tokens,
        system_tokens=sys_tokens,
        total_bytes=total_bytes,
        target_tokens=target_tokens,
        max_tokens=max_tokens,
        lineage_verified=lineage_verified,
        cache_hit=cache_hit,
        sections=sections,
        dropped_items=dropped_items,
        trimmed=trimmed,
    )

    logger.info(
        f"LLM Pre-Call Audit: tokens={total_tokens} (target={target_tokens}, max={max_tokens}) "
        f"lineage={lineage_verified} cache_hit={cache_hit} trimmed={trimmed} "
        f"sections={sections} dropped_count={len(dropped_items)}"
    )

    return bounded_prompt, bounded_sys, audit
