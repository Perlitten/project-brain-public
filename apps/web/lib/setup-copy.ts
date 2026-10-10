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
    why: "Your coding agent (Claude Code, Cursor, Codex…) gets a key and a config, then asks Brain for context through its tools instead of reading the whole repository.",
  },
  first_task: {
    title: "Try it on a real task",
    why: "Describe a task the way you would give it to an agent. Brain answers with a context pack: the files, rules and past decisions the agent needs for that task.",
  },
};

export const stepTitle = (id: string, fallback: string) => STEP_COPY[id]?.title ?? fallback;
