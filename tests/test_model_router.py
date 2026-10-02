import pytest
from unittest.mock import patch

from brain.config.settings import settings
from brain.llm.providers.mock_provider import (
    MockEmbeddingProvider,
    MockLLMProvider,
)
from brain.llm.providers.nvidia_provider import NvidiaLLMProvider, NvidiaSummarizerProvider
from brain.llm.router import ModelRouter, TaskKind, get_model_router


@pytest.fixture(autouse=True)
def _clear_router_cache():
    get_model_router.cache_clear()
    yield
    get_model_router.cache_clear()


def test_task_model_resolution():
    router = ModelRouter()
    assert router.task_model(TaskKind.CLASSIFICATION) == settings.LLM_TASK_CLASSIFICATION_MODEL
    assert router.task_model(TaskKind.SUMMARIZATION) == settings.LLM_TASK_SUMMARIZATION_MODEL
    assert router.task_model(TaskKind.SYNTHESIS) == settings.LLM_TASK_SYNTHESIS_MODEL
    assert router.task_model(TaskKind.INSIGHT) == settings.LLM_TASK_INSIGHT_MODEL


def test_mock_provider_same_llm_for_all_tasks():
    with patch.object(settings, "DEFAULT_LLM_PROVIDER", "mock"):
        router = ModelRouter()
        assert isinstance(router.llm(TaskKind.CLASSIFICATION), MockLLMProvider)
        assert isinstance(router.llm(TaskKind.SYNTHESIS), MockLLMProvider)
        assert router.llm(TaskKind.CLASSIFICATION) is router.llm(TaskKind.SYNTHESIS)


def test_nvidia_llm_per_task_model():
    with patch.object(settings, "DEFAULT_LLM_PROVIDER", "nvidia"), patch.object(
        settings, "LLM_TASK_CLASSIFICATION_MODEL", "meta/llama-3.1-8b-instruct"
    ), patch.object(settings, "LLM_TASK_SYNTHESIS_MODEL", "meta/llama-3.1-70b-instruct"):
        router = ModelRouter()
        classification = router.llm(TaskKind.CLASSIFICATION)
        synthesis = router.llm(TaskKind.SYNTHESIS)
        assert isinstance(classification, NvidiaLLMProvider)
        assert isinstance(synthesis, NvidiaLLMProvider)
        assert classification.model == "meta/llama-3.1-8b-instruct"
        assert synthesis.model == "meta/llama-3.1-70b-instruct"
        assert classification is not synthesis


def test_summarizer_uses_summarization_model():
    with patch.object(settings, "DEFAULT_LLM_PROVIDER", "nvidia"), patch.object(
        settings, "LLM_TASK_SUMMARIZATION_MODEL", "meta/llama-3.1-8b-instruct"
    ):
        router = ModelRouter()
        summarizer = router.summarizer()
        assert isinstance(summarizer, NvidiaSummarizerProvider)
        assert summarizer.llm.model == "meta/llama-3.1-8b-instruct"


def test_embedding_provider_mock():
    with patch.object(settings, "DEFAULT_EMBEDDING_PROVIDER", "mock"):
        router = ModelRouter()
        assert isinstance(router.embedding("query"), MockEmbeddingProvider)
        assert router.embedding("query") is router.embedding("passage")


def test_routing_table_and_api_keys():
    router = ModelRouter()
    table = router.routing_table()
    assert len(table) == 4
    assert {row["task_kind"] for row in table} == {
        "classification",
        "summarization",
        "synthesis",
        "insight",
    }
    keys = router.api_key_status()
    assert "nvidia" in keys
    assert keys["nvidia"] in ("configured", "missing")


def test_dashboard_summary():
    summary = ModelRouter().dashboard_summary()
    assert "routing_table" in summary
    assert "api_keys" in summary
    assert summary["llm_provider"] == settings.DEFAULT_LLM_PROVIDER.lower()


@pytest.mark.asyncio
async def test_mock_summarizer_via_router():
    with patch.object(settings, "DEFAULT_LLM_PROVIDER", "mock"):
        router = ModelRouter()
        summary = await router.summarizer().summarize("def hello(): pass")
        assert "Mock summary" in summary
