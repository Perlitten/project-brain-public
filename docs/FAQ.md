# FAQ

## How is this different from Obsidian / Mem0 / Letta / plain RAG?

- **vs Obsidian** — Brain is not a notes app: it indexes *code* (chunks +
  dependency graph), and its memory is written by agents, for agents, through
  MCP tools. You never open a vault; your agent queries it.
- **vs other memory systems** — Brain combines project-scoped memory with an
  indexed code graph and task context. Other systems also offer memory and
  scoping; no head-to-head performance advantage has been established. Explicit
  rules and decisions can be recorded directly; learned guidance follows the
  [L1–L4 evidence and approval policy](MEMORY_LESSON_PROTOCOL.md).
- **vs plain RAG** — retrieval is only one tool of thirteen. The graph,
  impact analysis, drift detection and recorded decisions answer questions a
  vector store cannot ("what breaks if I change X").

## What does it cost in tokens?

Costs depend on the selected models, indexing, retrieval, follow-up source reads
and maintenance. Payload limits differ between tools and rollout modes; the
compact v2 locator is opt-in and is not the default legacy search response.
Brain adds no per-call fee, but provider and infrastructure costs remain yours.
Net coding token, time or cost savings have **not been established**.

## Does it work offline?

After downloading models and dependencies, configure **both** LLM and embedding
providers as local Ollama endpoints for local model execution. For example,
use `DEFAULT_LLM_PROVIDER=ollama`, `DEFAULT_EMBEDDING_PROVIDER=ollama`,
`LLM_MODEL=llama3.1` and `EMBEDDING_MODEL=nomic-embed-text`. Check that endpoint
overrides are local, disable external notifications/integrations, and use a local
agent connection. Mock providers also run offline, but their results are only a
smoke test. Changing embedding models requires compatibility checks/reindexing.

## Where does my data go?

The code index and memory are stored in the databases you operate. This does
**not** mean source code always stays on that machine:

- External embedding providers receive text chunks selected for embedding.
- External LLM providers receive prompts that can include retrieved source,
  decisions, rules, lessons and task descriptions.
- Connected agents receive tool results; their own model/service policies apply.
- Optional notifications, webhooks and remote services can transmit their payloads.

For local model execution, configure both provider roles and all endpoint
overrides to locally hosted models. Cloud-provider configurations are not a
local-only guarantee. Review provider retention policies before indexing private
code. Keep server-side credentials and `.env` files private; scoped keys and OAuth
control access but do not prevent an authorized recipient from receiving context.

## Do learned lessons always require approval?

No. L1 observations/lessons record reported evidence, not proof Brain executed
the validation. Eligible L3 clusters can promote automatically by default,
subject to evidence, severity and contradiction gates. Set
`MEMORY_CONSOLIDATION_REQUIRE_APPROVAL=true` for approval of new promotions;
duplicate reinforcement has separate behavior. Distilled L4 procedures default
to pending approval. Direct rules, decisions and curated procedures are distinct
write paths. See the [complete policy](MEMORY_LESSON_PROTOCOL.md).

## Do I need Neo4j?

Only for graph-dependent tools (impact, couplings). The API and search run
without it; tools degrade gracefully.

## Is this production-ready?

It is early single-user, self-hosted software for a trusted operator, not a
multi-tenant team service. Main contains 0.9.1 development code; the latest
published release is v0.9.0. Public exposure requires TLS and the scoped-key
discipline in DEPLOYMENT.md. Production operation and useful coding outcomes
must be evaluated for your own workflow; passing tests do not establish them.
