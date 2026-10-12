"""NIM treats "data:image/...;base64," inside embedding text as an image and 400s."""

import pytest

from brain.llm.providers.nvidia_provider import NvidiaEmbeddingProvider


@pytest.mark.asyncio
async def test_image_data_urls_are_neutralized_before_sending(monkeypatch):
    provider = NvidiaEmbeddingProvider(api_key="nvapi-test", model="nvidia/nemotron-3-embed-1b")
    sent = []

    async def fake_post(api_url, headers, payload, n):
        sent.extend(payload["input"])
        return [[0.0] * provider.dimension for _ in range(n)]

    monkeypatch.setattr(provider, "_post", fake_post)
    code = 'huge = "data:image/jpeg;base64," + "A" * 5'
    await provider.embed_batch([code, "url = 'DATA:Image/png;base64,AAAA'", "plain = 'data:text/plain;base64,AA'"])

    assert sent[0] == 'huge = "data: image/jpeg;base64," + "A" * 5'
    assert "data: image/png" in sent[1]
    assert sent[2] == "plain = 'data:text/plain;base64,AA'"
    assert all("data:image/" not in t.lower() for t in sent)
