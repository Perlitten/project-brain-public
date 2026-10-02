import hashlib
import json
import math
import re
from collections import Counter
from typing import List, Optional

from brain.llm.providers.base import LLMProvider, EmbeddingProvider, SummarizerProvider


def _deterministic_vector(text: str, dimension: int = 1536) -> List[float]:
    """Build a normalized bag-of-words style vector for mock semantic search."""
    vec = [0.0] * dimension
    tokens = re.findall(r"\b[a-z0-9_]{2,}\b", text.lower())
    if not tokens:
        return vec

    counts = Counter(tokens)
    for token, count in counts.items():
        tf = 1.0 + math.log(count)
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        for i in range(12):
            idx = int.from_bytes(digest[i * 4 : i * 4 + 4], "big") % dimension
            vec[idx] += tf
        if len(token) > 4:
            for i in range(4):
                idx = int.from_bytes(digest[i : i + 4], "big") % dimension
                vec[idx] += tf * 0.5

    magnitude = math.sqrt(sum(value * value for value in vec))
    if magnitude:
        vec = [value / magnitude for value in vec]
    return vec


class MockLLMProvider(LLMProvider):
    """Mock implementation of LLMProvider that returns structured JSON when possible."""

    async def generate(
        self,
        prompt: str,
        system_instruction: Optional[str] = None,
        **kwargs,
    ) -> str:
        from brain.llm.observability import audit_and_bound_llm_input

        prompt, system_instruction, audit = audit_and_bound_llm_input(
            prompt,
            system_instruction,
            lineage_verified=kwargs.pop("lineage_verified", True),
            cache_hit=kwargs.pop("cache_hit", False),
        )

        prompt_lower = prompt.lower()
        if "json" in prompt_lower or "respond only" in prompt_lower:
            if "proactive insights" in prompt_lower or "operational snapshot" in prompt_lower:
                return json.dumps(
                    {
                        "insights": [
                            {
                                "insight_type": "mock_llm_review",
                                "severity": "info",
                                "title": "Mock LLM reviewed the operational snapshot",
                                "summary": "The mock model found a bounded operational observation with cited evidence.",
                                "evidence": [{"label": "provider", "value": "mock"}],
                                "recommended_action": "Keep deterministic checks as the fallback path.",
                                "confidence": "medium",
                                "dedupe_key": "mock_llm_review:provider_mock",
                            }
                        ]
                    }
                )
            if "task type" in prompt_lower or "task description" in prompt_lower:
                keywords = extract_keywords_from_prompt(prompt)
                task_type = "feature"
                if any(t in prompt_lower for t in ("branding", "template", "styling", "dashboard", "html")):
                    task_type = "design_change"
                elif any(t in prompt_lower for t in ("bug", "fix", "health", "unavailable")):
                    task_type = "bugfix"
                elif "refactor" in prompt_lower or "dead code" in prompt_lower:
                    task_type = "refactor"
                elif "retrieval" in prompt_lower or "context pack" in prompt_lower:
                    task_type = "feature"
                return json.dumps(
                    {
                        "task_type": task_type,
                        "keywords": keywords,
                        "risks": ["regression"],
                        "features": ["core"],
                    }
                )
            if "implementation plan" in prompt_lower or "checklist" in prompt_lower:
                return (
                    '{"plan": "1. Review relevant files\\n2. Apply changes\\n3. Run tests", '
                    '"checklist": ["Run pytest", "Verify API health"], '
                    '"risks": ["Unintended side effects"]}'
                )
            if "keywords" in prompt_lower and "json array" in prompt_lower:
                keywords = extract_keywords_from_prompt(prompt)
                return str(keywords)
            if "risk" in prompt_lower:
                return (
                    '{"adjusted_risk_level": "Medium", '
                    '"rationale": "Mock impact analysis", '
                    '"verification_checks": ["Run tests"]}'
                )
            if "rule_violations" in prompt_lower or "diff" in prompt_lower:
                return (
                    '{"rule_violations": [], "suspicious_changes": [], '
                    '"feedback": "Mock review passed"}'
                )
        sys_str = f" [System: {system_instruction}]" if system_instruction else ""
        return f"Mock response to prompt: '{prompt[:120]}'{sys_str}."


def extract_keywords_from_prompt(prompt: str) -> List[str]:
    words = re.findall(r"\b[a-zA-Z]{3,}\b", prompt)
    stopwords = {
        "the", "and", "for", "that", "this", "with", "from", "should", "would",
        "could", "about", "task", "description", "following", "analyze", "perform",
        "respond", "only", "json", "object", "containing", "keys", "given",
    }
    seen = set()
    keywords: List[str] = []
    for word in words:
        token = word.lower()
        if token in stopwords or token in seen:
            continue
        seen.add(token)
        keywords.append(token)
        if len(keywords) >= 8:
            break
    return keywords or ["project", "brain"]


class MockEmbeddingProvider(EmbeddingProvider):
    """Deterministic hash-based embeddings for local development and tests.

    Unlike a flat constant vector, these embeddings preserve lexical overlap
    so hybrid retrieval can rank relevant files above unrelated noise.
    """

    def __init__(self, dimension: int = 1536):
        self.dimension = dimension
        self.model = "mock-deterministic"
        self.provider = "mock"

    async def embed(self, text: str, **kwargs) -> List[float]:
        return _deterministic_vector(text, self.dimension)

    async def embed_batch(self, texts: List[str], **kwargs) -> List[List[float]]:
        return [_deterministic_vector(text, self.dimension) for text in texts]


class MockSummarizerProvider(SummarizerProvider):
    """Mock implementation of SummarizerProvider that returns a dummy summary."""

    async def summarize(self, text: str, max_length: Optional[int] = None, **kwargs) -> str:
        truncated = text[:50] + "..." if len(text) > 50 else text
        return f"Mock summary of: '{truncated}' (len: {len(text)})"
