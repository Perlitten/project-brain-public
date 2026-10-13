// Plain-language copy for the setup steps. The API's step titles are written
// for operators; these say what each step does and why it matters, so the
// page reads without hovering anything.

export interface StepCopy {
  title: string;
  why: string;
}

export const STEP_COPY: Record<string, StepCopy> = {
  services: {
    title: "Start Brain’s services",
    why: "Brain keeps its memory in a database, a search index and a code graph, and runs background workers. All of them must be running before anything else works.",
  },
  repo: {
    title: "Choose the code to read",
    why: "The folder on the Brain server that holds your repository. Brain reads it in place; it never copies or changes your files.",
  },
  provider: {
    title: "Connect AI models",
    why: "An embedding model turns code into something searchable; a language model writes short summaries of it. Both run on your own API keys.",
  },
  indexed: {
    title: "Read the code",
    why: "Brain reads every file once, splits it into searchable pieces and remembers which revision it read. Later runs only re-read what changed.",
  },
  agent: {
    title: "Connect your agent",
    why: "Your coding agent (Claude Code, Cursor, Codex…) gets a key and a config, then can ask Brain for context through its tools. It still needs to inspect source and validate changes.",
  },
  first_task: {
    title: "Try it on a real task",
    why: "Describe a task the way you would give it to an agent. Brain selects files, rules and decisions for a context pack; the selection may be incomplete or noisy. Verify it against current source. Build a new pack for each task and reindex committed source changes. This step checks that a pack can be returned; it does not establish coding or cost improvements.",
  },
};

export const stepTitle = (id: string, fallback: string) => STEP_COPY[id]?.title ?? fallback;

/** Repositories with a completed index run, as /api/setup/status reports them. */
export function indexedRepositories(step: { extra: Record<string, unknown> }): { name: string; path: string }[] {
  const list = step.extra.indexed_repositories;
  if (!Array.isArray(list)) return [];
  return list
    .map((r) => (r && typeof r === "object" ? (r as Record<string, unknown>) : {}))
    .filter((r) => typeof r.path === "string" && r.path)
    .map((r) => ({ name: typeof r.name === "string" && r.name ? r.name : String(r.path), path: String(r.path) }));
}
