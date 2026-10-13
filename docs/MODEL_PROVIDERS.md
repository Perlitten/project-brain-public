# Model providers

Pick providers in `.env`, set the matching key, then re-index:

| Provider | `.env` |
|---|---|
| Mock (default, offline) — smoke test only, results not meaningful | `DEFAULT_LLM_PROVIDER=mock`, `DEFAULT_EMBEDDING_PROVIDER=mock` |
| Ollama (local, free, no key) | `ollama pull nomic-embed-text`, then `DEFAULT_EMBEDDING_PROVIDER=ollama` (add `ollama pull llama3.1` and `DEFAULT_LLM_PROVIDER=ollama` for summaries) |
| NVIDIA NIM (hosted option) | `DEFAULT_LLM_PROVIDER=nvidia`, `DEFAULT_EMBEDDING_PROVIDER=nvidia`, `NVIDIA_API_KEY=nvapi-…` |
| OpenAI / Anthropic / Google | `DEFAULT_LLM_PROVIDER=openai` (or `anthropic`, `google`) and the matching `*_API_KEY` |

Embeddings from different providers are not comparable. After switching the
embedding provider run `brain embeddings verify` and, if it reports
incompatible vectors, `brain embeddings backfill`.

#### Any OpenAI-compatible provider

LLM, summarizer and embedding calls go through one generic OpenAI-compatible
client (`brain/llm/providers/openai_compatible.py`), so any API that speaks
`/v1/chat/completions` and `/v1/embeddings` works: OpenAI, OpenRouter, Groq,
Together, DeepSeek, Mistral, Fireworks, NVIDIA NIM, Ollama, LM Studio, vLLM.
`mock`, `anthropic` and `google` keep their native adapters.

`DEFAULT_LLM_PROVIDER` / `DEFAULT_EMBEDDING_PROVIDER` take a preset name
(`openai`, `nvidia`, `openrouter`, `groq`, `together`, `deepseek`, `mistral`,
`ollama`, `lmstudio`) or `openai_compatible` (aliases `custom`,
`openai-compatible`). Presets only supply defaults (`brain/llm/presets.py`);
these universal settings override them for the selected provider:

| Variable | Meaning |
|---|---|
| `LLM_BASE_URL` | API root incl. version path, e.g. `https://openrouter.ai/api/v1` |
| `LLM_API_KEY` | Bearer key; falls back to the preset key (`GROQ_API_KEY`, ...). Optional for local endpoints |
| `LLM_MODEL` / `SUMMARIZER_MODEL` | main (synthesis) model / cheaper model for summaries & classification |
| `EMBEDDING_BASE_URL` / `EMBEDDING_API_KEY` | default to `LLM_*` when both roles use the same provider |
| `EMBEDDING_MODEL` / `EMBEDDING_DIMENSION` | dimension is required for non-preset models and must match the pgvector column |
| `EMBEDDING_MAX_INPUT_CHARS` / `EMBEDDING_BATCH_SIZE` | per-input char cap / inputs per request |

`LLM_TASK_*_MODEL` still override per-task routing. Examples:

```bash
# OpenRouter (chat); embeddings stay on another provider
DEFAULT_LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-...
LLM_MODEL=anthropic/claude-3.5-sonnet

# Groq
DEFAULT_LLM_PROVIDER=groq
GROQ_API_KEY=gsk_...
SUMMARIZER_MODEL=llama-3.1-8b-instant

# Ollama, fully local, no key (768-dim nomic-embed-text)
DEFAULT_LLM_PROVIDER=ollama
DEFAULT_EMBEDDING_PROVIDER=ollama
LLM_MODEL=llama3.1
```

Changing the embedding provider/model/dimension on an existing database
requires a full re-index; `brain doctor` fails early when the configured
dimension is unknown or contradicts the preset, and the embedding client
rejects vectors of the wrong width.


Before indexing private source, review the [data boundaries](ARCHITECTURE.md#trust-and-data-boundaries). Return to [getting started](GETTING_STARTED.md).
