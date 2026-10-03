import pytest
from brain.database.models import Base
from brain.llm import (
    get_llm_provider,
    get_embedding_provider,
    get_summarizer_provider
)
from brain.llm.providers.mock_provider import (
    MockLLMProvider,
    MockEmbeddingProvider,
    MockSummarizerProvider
)
from brain.llm.providers.openai_provider import (
    OpenAILLMProvider,
    OpenAIEmbeddingProvider
)
from brain.llm.providers.anthropic_provider import (
    AnthropicLLMProvider,
    AnthropicEmbeddingProvider
)
from brain.llm.providers.google_provider import (
    GoogleLLMProvider,
    GoogleEmbeddingProvider
)

def test_db_models_import():
    # Verify metadata contains our 11 tables
    expected_tables = {
        "repositories", "files", "file_chunks", "symbols", "decisions",
        "rules", "context_packs", "indexing_runs", "embeddings", "tasks",
        "diff_reviews", "insights"
    }
    assert expected_tables.issubset(Base.metadata.tables.keys())


@pytest.mark.asyncio
async def test_mock_llm_provider():
    llm = get_llm_provider("mock")
    assert isinstance(llm, MockLLMProvider)

    res = await llm.generate("Hello world", system_instruction="Be helpful")
    assert "Mock response to prompt" in res
    assert "Hello world" in res
    assert "System: Be helpful" in res


@pytest.mark.asyncio
async def test_mock_embedding_provider():
    embedder = get_embedding_provider("mock")
    assert isinstance(embedder, MockEmbeddingProvider)

    vec = await embedder.embed("test text")
    assert len(vec) == 1536
    assert any(val != 0 for val in vec)

    similar = await embedder.embed("test text")
    assert vec == similar

    different = await embedder.embed("completely different content")
    assert vec != different

    vecs = await embedder.embed_batch(["text1", "text2"])
    assert len(vecs) == 2
    assert len(vecs[0]) == 1536
    assert len(vecs[1]) == 1536


@pytest.mark.asyncio
async def test_mock_summarizer_provider():
    summarizer = get_summarizer_provider("mock")
    assert isinstance(summarizer, MockSummarizerProvider)

    summary = await summarizer.summarize("A long piece of text that needs to be shortened.")
    assert "Mock summary of" in summary


@pytest.mark.asyncio
async def test_real_providers_error_on_missing_keys():
    # OpenAI
    openai_llm = OpenAILLMProvider(api_key="")
    with pytest.raises(ValueError, match="OpenAI API key is not configured"):
        await openai_llm.generate("test")

    openai_emb = OpenAIEmbeddingProvider(api_key="")
    with pytest.raises(ValueError, match="OpenAI API key is not configured"):
        await openai_emb.embed("test")

    # Anthropic
    anthropic_llm = AnthropicLLMProvider(api_key="")
    with pytest.raises(ValueError, match="Anthropic API key is not configured"):
        await anthropic_llm.generate("test")

    anthropic_emb = AnthropicEmbeddingProvider()
    with pytest.raises(NotImplementedError, match="does not offer a native embedding API"):
        await anthropic_emb.embed("test")

    # Google
    google_llm = GoogleLLMProvider(api_key="")
    with pytest.raises(ValueError, match="Google API key is not configured"):
        await google_llm.generate("test")

    google_emb = GoogleEmbeddingProvider(api_key="")
    with pytest.raises(ValueError, match="Google API key is not configured"):
        await google_emb.embed("test")

    # NVIDIA
    from brain.llm.providers.nvidia_provider import NvidiaLLMProvider, NvidiaEmbeddingProvider

    nvidia_llm = NvidiaLLMProvider(api_key="")
    with pytest.raises(ValueError, match="NVIDIA API key is not configured"):
        await nvidia_llm.generate("test")

    nvidia_emb = NvidiaEmbeddingProvider(api_key="")
    with pytest.raises(ValueError, match="NVIDIA API key is not configured"):
        await nvidia_emb.embed("test")


@pytest.mark.asyncio
async def test_get_nvidia_providers():
    from brain.llm.providers.nvidia_provider import NvidiaLLMProvider, NvidiaEmbeddingProvider, NvidiaSummarizerProvider

    llm = get_llm_provider("nvidia")
    assert isinstance(llm, NvidiaLLMProvider)

    embedder = get_embedding_provider("nvidia")
    assert isinstance(embedder, NvidiaEmbeddingProvider)

    summarizer = get_summarizer_provider("nvidia")
    assert isinstance(summarizer, NvidiaSummarizerProvider)


def test_nvidia_embedding_dimension_follows_setting(monkeypatch):
    from brain.config.settings import settings
    from brain.llm.providers.nvidia_provider import NvidiaEmbeddingProvider

    monkeypatch.setattr(settings, "EMBEDDING_DIMENSION", 0)
    assert NvidiaEmbeddingProvider(api_key="k").dimension == 4096
    monkeypatch.setattr(settings, "EMBEDDING_DIMENSION", 2048)
    assert NvidiaEmbeddingProvider(api_key="k", model="nvidia/nemotron-3-embed-1b").dimension == 2048
