# Model Orchestration

Project Brain routes LLM calls by **task kind** through `ModelRouter` (`brain/llm/router.py`). Low-level `get_llm_provider()` remains available as a fallback for scripts and legacy paths.

## Task → model routing

| Task kind | Used for | Default NVIDIA model |
|-----------|----------|----------------------|
| `classification` | Task type / keywords JSON, impact keyword extraction | `meta/llama-3.1-8b-instruct` |
| `summarization` | File and chunk summaries during indexing | `meta/llama-3.1-8b-instruct` |
| `synthesis` | Ask, context-pack plan, impact report, diff review | `meta/llama-3.1-70b-instruct` |

Embeddings use `DEFAULT_EMBEDDING_PROVIDER` (typically `nvidia`) with `NVIDIA_EMBEDDING_MODEL` (`nvidia/nv-embedcode-7b-v1`). Query vs passage mode is passed at call time via `input_type`.

## Environment variables

```env
DEFAULT_LLM_PROVIDER=nvidia
DEFAULT_EMBEDDING_PROVIDER=nvidia
NVIDIA_API_KEY=nvapi-...

LLM_TASK_CLASSIFICATION_MODEL=meta/llama-3.1-8b-instruct
LLM_TASK_SUMMARIZATION_MODEL=meta/llama-3.1-8b-instruct
LLM_TASK_SYNTHESIS_MODEL=meta/llama-3.1-70b-instruct

NVIDIA_EMBEDDING_MODEL=nvidia/nv-embedcode-7b-v1
```

Use `DEFAULT_LLM_PROVIDER=mock` and `DEFAULT_EMBEDDING_PROVIDER=mock` for local development without API keys.

## Re-index after embedding changes

Changing `DEFAULT_EMBEDDING_PROVIDER`, `NVIDIA_EMBEDDING_MODEL`, or `EMBEDDING_DIMENSION` produces vectors incompatible with existing rows. After such a change:

1. Run `brain embeddings verify` to inspect coverage and mismatches.
2. Re-index the target repository (`brain index` or `/index` API) so chunks get fresh embeddings and pgvector columns.

Summarization model changes only affect new summaries; a full re-index refreshes file/chunk summary text as well.

## Dashboard

The **Overview** cockpit shows the routing table (task → model), embedding provider/model, and API key status (`configured` / `missing`) without exposing secret values.
