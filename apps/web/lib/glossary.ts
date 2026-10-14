// One plain sentence per term. Anything dotted-underlined in the UI reads here.
export const glossary = {
  chunk: "A small piece of a file — a function, a class or a few paragraphs. Brain stores code in pieces so it can find exactly the right one.",
  searchable: "Brain has turned this piece into a meaning fingerprint (an embedding), so it can be found by what it does, not just by its exact words.",
  index: "Brain reading your repository: files, functions and how they connect. Like a book's index, it must be redone when the book changes.",
  pack: "A context pack selects files, rules and decisions for one task. It can miss relevant evidence; agents still need to inspect current source.",
  reranker: "A second, more careful pass over search results that re-orders them by how well they actually match the question.",
  mcp: "Model Context Protocol — the standard plug that lets AI agents like Claude Code or Codex call Brain's tools.",
  agent: "An AI coding assistant (Claude Code, Codex, Cursor, Devin) that asks Brain for context before changing code.",
  principal: "Anyone or anything that can sign in to Brain — a person, an AI agent or a service — each with its own revocable key.",
  scope: "What a key is allowed to do, e.g. read code (core:read) or record decisions (memory:write).",
  drift: "The code structure moving away from the architecture you agreed on.",
  coupling: "Two parts of the code that keep changing together — a sign they are more tangled than they look.",
  hotspot: "A file that changes far more often than the rest. Bugs cluster here.",
  golden: "A fixed set of curated questions or tasks with expected answers. Results describe that dataset, not general coding effectiveness.",
  p95: "95% of calls finish faster than this. The slowest 5% are slower.",
  freshness: "How recently Brain re-read this repository. When Brain can't reach the source it can't tell whether its copy is current — answers come from the last version it read.",
  degraded: "The job finished but not completely — part of the work was skipped or fell back to a simpler path. Results are usable, just weaker.",
  tokens: "Units a model tokenizer assigns to text. Counts vary by model; smaller context alone does not establish lower total cost or faster coding.",
} as const;

export type GlossaryKey = keyof typeof glossary;
