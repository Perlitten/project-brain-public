# FAQ

## How is this different from Obsidian / Mem0 / Letta / plain RAG?

- **vs Obsidian** — Brain is not a notes app: it indexes *code* (chunks +
  dependency graph), and its memory is written by agents, for agents, through
  MCP tools. You never open a vault; your agent queries it.
- **vs Mem0 / Letta** — they store conversational memory for a persona. Brain's
  L1–L4 memory (learnings, skills, rules, decisions) is project-scoped and
  evidence-graded: a rule exists because a gate admitted it, not because the
  model remembered.
- **vs plain RAG** — retrieval is only one tool of thirteen. The graph,
  impact analysis, drift detection and recorded decisions answer questions a
  vector store cannot ("what breaks if I change X").

## What does it cost in tokens?

Context packs are budgeted (~47k tokens for a typical 10-file pack in the
external eval). `brain_ask` returns a bounded answer, not a file dump. Costs
are yours: your LLM/embedding provider keys — Brain itself adds no per-call
fee. Fully local is possible with Ollama embeddings.

## Does it work offline?

Yes with Ollama (`nomic-embed-text`, 768-dim) — indexing, search and memory
work without any provider key. LLM-dependent features (summarisation,
reranking quality) degrade to local models you configure.

## Where does my data go?

Nowhere. Postgres, Neo4j and Redis run in your Docker network; the API binds
loopback unless you expose it. Keys stay in `.env`, never logged. Remote
agents reach the MCP endpoint through scoped `pbk_` credentials or OAuth that
you mint and can revoke.

## Do I need Neo4j?

Only for graph-dependent tools (impact, couplings). The API and search run
without it; tools degrade gracefully.

## Is this production-ready?

0.9.x: single-user / small-team self-host. The auth model assumes a trusted
operator; don't expose the API publicly without nginx + the scoped-key
discipline in DEPLOYMENT.md.
