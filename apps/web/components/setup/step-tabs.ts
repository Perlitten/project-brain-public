// Short labels for the Setup stepper's tabs; each step keeps its full title
// (lib/setup-copy.ts) on the step itself. A plain module so the server page can read it.
export const STEP_TAB: Record<string, string> = {
  services: "Services",
  repo: "Repository",
  provider: "Models",
  indexed: "Index",
  agent: "Agent",
  first_task: "First task",
};
