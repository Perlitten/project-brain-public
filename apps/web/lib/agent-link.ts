// Agents with a name Brain can file under get their own page (/agents/<name>).
const AGENT_SLUG = /^[a-z0-9][a-z0-9._-]*$/i;

export const agentHref = (name: string): string | null =>
  AGENT_SLUG.test(name) && name !== "agent" && name !== "unknown" ? `/agents/${encodeURIComponent(name)}` : null;
